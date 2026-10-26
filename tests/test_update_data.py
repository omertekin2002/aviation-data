import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import update_data  # noqa: E402
from update_data import (  # noqa: E402
    BASE_DELAY_SECONDS,
    MAX_ATTEMPTS,
    clean_number,
    fetch_symbol,
    load_existing,
    run,
)

NAN = float("nan")
INF = float("inf")


class FakeSeries:
    def __init__(self, values):
        self.values = list(values)

    @property
    def empty(self):
        return not self.values

    def dropna(self):
        return FakeSeries(v for v in self.values if v is not None and v == v)

    @property
    def iloc(self):
        return self.values


class FakeHistory:
    def __init__(self, closes):
        self.closes = FakeSeries(closes)

    @property
    def empty(self):
        return self.closes.empty

    def __getitem__(self, column):
        assert column == "Close"
        return self.closes


class FakeTicker:
    def __init__(self, fast=None, info=None, closes=(), error=None):
        self._fast = fast if fast is not None else {}
        self._info = info if info is not None else {}
        self._closes = closes
        self._error = error

    @property
    def fast_info(self):
        if self._error:
            raise self._error
        return self._fast

    def get_info(self):
        if self._error:
            raise self._error
        return self._info

    def history(self, **kwargs):
        if self._error:
            raise self._error
        return FakeHistory(self._closes)


def factory(tickers):
    """Build a ticker factory from {symbol: FakeTicker or callable}."""
    calls = []

    def make(symbol):
        calls.append(symbol)
        ticker = tickers[symbol]
        return ticker() if callable(ticker) else ticker

    make.calls = calls
    return make


def quiet(func, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()) as err:
        result = func(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


GOOD_INFO = {"currency": "USD", "marketCap": 1000, "trailingPE": 9.5}


class CleanNumberTests(unittest.TestCase):
    def test_none_nan_and_inf_become_none(self):
        for value in (None, NAN, INF, -INF):
            with self.subTest(value=value):
                self.assertIsNone(clean_number(value))
                self.assertIsNone(clean_number(value, 2))

    def test_integer_mode_truncates(self):
        self.assertEqual(clean_number(5581246873.9), 5581246873)
        self.assertIsInstance(clean_number(12.0), int)

    def test_decimals_round(self):
        self.assertEqual(clean_number(84.8749, 2), 84.87)
        self.assertEqual(clean_number(84.875001, 2), 84.88)
        self.assertEqual(clean_number("41.5", 2), 41.5)

    def test_non_numeric_raises(self):
        with self.assertRaises(ValueError):
            clean_number("abc")


class FetchSymbolTests(unittest.TestCase):
    def fetch(self, ticker):
        result, _, _ = quiet(fetch_symbol, "SYM", factory({"SYM": ticker}))
        return result

    def test_price_fallback_order(self):
        full_info = dict(
            GOOD_INFO, currentPrice=2.0, regularMarketPrice=3.0, previousClose=4.0
        )
        cases = [
            ({"last_price": 1.0}, full_info, [5.0], 1.0),
            ({"last_price": NAN}, full_info, [5.0], 2.0),
            ({}, dict(full_info, currentPrice=None), [5.0], 3.0),
            ({}, dict(GOOD_INFO, previousClose=4.0), [5.0], 4.0),
            ({}, GOOD_INFO, [5.0, 6.0, NAN], 6.0),
        ]
        for fast, info, closes, expected in cases:
            with self.subTest(expected=expected):
                out = self.fetch(FakeTicker(fast=fast, info=info, closes=closes))
                self.assertEqual(out["stockPrice"], expected)

    def test_pe_falls_back_from_trailing_to_forward(self):
        info = dict(GOOD_INFO, currentPrice=1, trailingPE=None, forwardPE=7.123)
        self.assertEqual(self.fetch(FakeTicker(info=info))["peRatio"], 7.12)
        info["forwardPE"] = INF
        self.assertIsNone(self.fetch(FakeTicker(info=info))["peRatio"])

    def test_market_cap_and_currency_fall_back_to_fast_info(self):
        out = self.fetch(
            FakeTicker(
                fast={"last_price": 10.5, "market_cap": 1234.9, "currency": "GBp"},
                info={},
            )
        )
        self.assertEqual(
            out,
            {"stockPrice": 10.5, "currency": "GBp", "marketCap": 1234, "peRatio": None},
        )

    def test_missing_required_fields_raise(self):
        with self.assertRaisesRegex(RuntimeError, "stockPrice, marketCap, currency"):
            self.fetch(FakeTicker())
        with self.assertRaisesRegex(RuntimeError, "missing required fields: currency"):
            self.fetch(FakeTicker(info={"currentPrice": 1, "marketCap": 2, "currency": ""}))

    def test_errors_from_yahoo_are_warnings_not_failures(self):
        class HalfBroken(FakeTicker):
            @property
            def fast_info(self):
                raise KeyError("boom")

            def history(self, **kwargs):
                raise ValueError("no history")

        result, out, _ = quiet(
            fetch_symbol,
            "SYM",
            factory({"SYM": HalfBroken(info=dict(GOOD_INFO, currentPrice=3))}),
        )
        self.assertEqual(result["stockPrice"], 3)
        self.assertIn("::warning::SYM: fast_info failed", out)
        self.assertIn("::warning::SYM: history fallback failed", out)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.sleeps = []

    def run_updater(self, symbols, tickers):
        make = factory(tickers)
        code, out, err = quiet(
            run,
            data_dir=self.tmp,
            ticker_factory=make,
            sleep=self.sleeps.append,
            symbols=symbols,
        )
        return code, out, err, make.calls

    def test_success_writes_exact_format(self):
        code, out, _, _ = self.run_updater(
            {"abc": "ABC"},
            {"ABC": FakeTicker(info=dict(GOOD_INFO, currentPrice=12.345678))},
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            (self.tmp / "abc.json").read_text(),
            '{\n  "stockPrice": 12.35,\n  "currency": "USD",\n'
            '  "marketCap": 1000,\n  "peRatio": 9.5\n}\n',
        )
        self.assertIn("ABC: 12.35 USD | market cap 1000 | P/E 9.5", out)

    def test_output_is_byte_identical_to_existing_data_files(self):
        for key in ("dal", "2618", "wizz"):
            with self.subTest(key=key):
                source = REPO_ROOT / "data" / f"{key}.json"
                record = json.loads(source.read_text())
                shutil.copy(source, self.tmp / f"{key}.json")
                info = {
                    "currentPrice": record["stockPrice"],
                    "currency": record["currency"],
                    "marketCap": record["marketCap"],
                    "trailingPE": record["peRatio"],
                }
                symbol = update_data.stocks[key]
                code, _, _, _ = self.run_updater(
                    {key: symbol}, {symbol: FakeTicker(info=info)}
                )
                self.assertEqual(code, 0)
                self.assertEqual(
                    (self.tmp / f"{key}.json").read_bytes(), source.read_bytes()
                )

    def test_failing_symbol_with_valid_existing_file_keeps_it(self):
        existing = '{"stockPrice": 1.5, "currency": "EUR", "marketCap": 9, "peRatio": null}'
        (self.tmp / "bad.json").write_text(existing)
        code, out, _, calls = self.run_updater(
            {"bad": "BAD"}, {"BAD": FakeTicker(error=RuntimeError("down"))}
        )
        self.assertEqual(code, 0)
        self.assertEqual((self.tmp / "bad.json").read_text(), existing)
        self.assertEqual(calls, ["BAD"] * MAX_ATTEMPTS)
        self.assertIn("::warning::BAD: all attempts failed; keeping existing", out)

    def test_retries_use_injected_sleep_with_backoff(self):
        attempts = iter(
            [FakeTicker(), FakeTicker(), FakeTicker(info=dict(GOOD_INFO, currentPrice=1))]
        )
        code, out, _, calls = self.run_updater(
            {"x": "X", "y": "Y"},
            {
                "X": lambda: next(attempts),
                "Y": FakeTicker(info=dict(GOOD_INFO, currentPrice=2)),
            },
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["X", "X", "X", "Y"])
        # Backoff before attempts 2 and 3, then the 1s pause between symbols.
        self.assertEqual(self.sleeps, [BASE_DELAY_SECONDS, BASE_DELAY_SECONDS * 2, 1])
        self.assertIn(f"::warning::X: attempt 1/{MAX_ATTEMPTS} failed: RuntimeError", out)

    def test_all_attempts_failing_sleeps_between_each(self):
        self.run_updater({"bad": "BAD"}, {"BAD": FakeTicker()})
        self.assertEqual(
            self.sleeps,
            [BASE_DELAY_SECONDS * attempt for attempt in range(1, MAX_ATTEMPTS)],
        )

    def test_unrecoverable_symbol_fails_after_processing_the_rest(self):
        for existing in (None, '{"stockPrice": "1", "currency": "USD", "marketCap": 1, "peRatio": null}'):
            with self.subTest(existing=existing):
                for path in self.tmp.glob("*.json"):
                    path.unlink()
                if existing is not None:
                    (self.tmp / "bad.json").write_text(existing)
                code, out, err, calls = self.run_updater(
                    {"bad": "BAD", "good": "GOOD"},
                    {
                        "BAD": FakeTicker(error=RuntimeError("down")),
                        "GOOD": FakeTicker(info=dict(GOOD_INFO, currentPrice=3)),
                    },
                )
                self.assertEqual(code, 1)
                self.assertEqual(calls, ["BAD"] * MAX_ATTEMPTS + ["GOOD"])
                self.assertTrue((self.tmp / "good.json").exists())
                self.assertIn("::error::BAD: no valid existing data is available", out)
                self.assertIn("Unable to update symbols without valid fallback data: BAD", err)

    def test_default_factory_is_not_imported_eagerly(self):
        self.assertNotIn("yfinance", sys.modules)


class LoadExistingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def load(self, text):
        path = self.tmp / "x.json"
        path.write_text(text)
        return load_existing(path)

    def test_valid_file_is_returned(self):
        record = {"stockPrice": 1, "currency": "USD", "marketCap": 2, "peRatio": 3.5}
        self.assertEqual(self.load(json.dumps(record)), record)

    def test_invalid_files_are_rejected(self):
        for text in (
            "not json",
            "[]",
            '{"stockPrice": NaN, "currency": "USD", "marketCap": 2, "peRatio": null}',
            '{"stockPrice": 1, "currency": "", "marketCap": 2, "peRatio": null}',
            '{"stockPrice": 1, "currency": "USD", "marketCap": 2.5, "peRatio": null}',
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.load(text))

    def test_missing_file_is_none(self):
        self.assertIsNone(load_existing(self.tmp / "nope.json"))


if __name__ == "__main__":
    unittest.main()
