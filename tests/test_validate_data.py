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

from update_data import stocks, validate_fx_record, validate_record  # noqa: E402
from validate_data import check_dir, main, validate_dir  # noqa: E402

VALID = {"stockPrice": 84.87, "currency": "USD", "marketCap": 5581246873, "peRatio": 14.07}
VALID_FX = {
    "base": "USD",
    "source": "Yahoo Finance",
    "updatedAt": "2026-10-26T13:30:00Z",
    "rates": {"EUR": 1.08, "GBp": 0.0126, "JPY": 0.0066, "USD": 1},
}


class ValidateRecordTests(unittest.TestCase):
    def test_valid_records(self):
        self.assertEqual(validate_record(VALID), [])
        self.assertEqual(validate_record(dict(VALID, peRatio=None)), [])
        self.assertEqual(validate_record(dict(VALID, stockPrice=3)), [])

    def test_each_rule(self):
        cases = {
            "stockPrice must be a finite number": [
                dict(VALID, stockPrice="84.87"),
                dict(VALID, stockPrice=None),
                dict(VALID, stockPrice=True),
                dict(VALID, stockPrice=float("nan")),
            ],
            "currency must be a non-empty string": [
                dict(VALID, currency=""),
                dict(VALID, currency=None),
                dict(VALID, currency=840),
            ],
            "marketCap must be an integer": [
                dict(VALID, marketCap=1.5),
                dict(VALID, marketCap="1"),
                dict(VALID, marketCap=False),
            ],
            "peRatio must be a finite number or null": [
                dict(VALID, peRatio="14"),
                dict(VALID, peRatio=float("inf")),
            ],
            "peRatio is missing": [
                {k: v for k, v in VALID.items() if k != "peRatio"},
            ],
        }
        for expected, records in cases.items():
            for record in records:
                with self.subTest(record=record):
                    problems = validate_record(record)
                    self.assertEqual(len(problems), 1, problems)
                    self.assertIn(expected, problems[0])

    def test_non_object(self):
        self.assertIn("expected a JSON object", validate_record([])[0])


class ValidateFxRecordTests(unittest.TestCase):
    def test_valid_records(self):
        self.assertEqual(validate_fx_record(VALID_FX), [])
        self.assertEqual(validate_fx_record(dict(VALID_FX, rates={"USD": 1.0})), [])

    def test_each_rule(self):
        cases = {
            "base must be 'USD'": [
                dict(VALID_FX, base="EUR"),
                {k: v for k, v in VALID_FX.items() if k != "base"},
            ],
            "source must be a non-empty string": [
                dict(VALID_FX, source=""),
                dict(VALID_FX, source=None),
            ],
            "updatedAt must be a UTC time": [
                dict(VALID_FX, updatedAt="2026-10-26 13:30"),
                dict(VALID_FX, updatedAt="2026-10-26T13:30:00+00:00"),
                dict(VALID_FX, updatedAt=1761485400),
            ],
            "rates must be a JSON object": [
                dict(VALID_FX, rates=[1.08]),
                {k: v for k, v in VALID_FX.items() if k != "rates"},
            ],
            "rates.EUR must be a finite number > 0": [
                dict(VALID_FX, rates={"EUR": 0, "USD": 1}),
                dict(VALID_FX, rates={"EUR": -1.08, "USD": 1}),
                dict(VALID_FX, rates={"EUR": "1.08", "USD": 1}),
                dict(VALID_FX, rates={"EUR": True, "USD": 1}),
                dict(VALID_FX, rates={"EUR": float("inf"), "USD": 1}),
                dict(VALID_FX, rates={"EUR": None, "USD": 1}),
            ],
            "rates.USD must be 1": [dict(VALID_FX, rates={"USD": 0.99})],
            "empty currency code": [dict(VALID_FX, rates={"": 1.0, "USD": 1})],
        }
        for expected, records in cases.items():
            for record in records:
                with self.subTest(record=record):
                    problems = validate_fx_record(record)
                    self.assertEqual(len(problems), 1, problems)
                    self.assertIn(expected, problems[0])

    def test_non_object(self):
        self.assertIn("expected a JSON object", validate_fx_record([])[0])


class ValidateDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "data"
        self.addCleanup(shutil.rmtree, self.tmp.parent)
        shutil.copytree(REPO_ROOT / "data", self.tmp)

    def write(self, name, text):
        (self.tmp / name).write_text(text)

    def assert_single_error(self, *fragments):
        errors = validate_dir(self.tmp)
        self.assertEqual(len(errors), 1, errors)
        for fragment in fragments:
            self.assertIn(fragment, errors[0])

    def test_real_data_passes(self):
        self.assertEqual(check_dir(REPO_ROOT / "data"), ([], []))

    def test_fx_file_is_expected(self):
        self.write("fx.json", json.dumps(VALID_FX))
        errors, _ = check_dir(self.tmp)
        self.assertEqual(errors, [])

    def test_malformed_fx_json(self):
        for text in ('{"base": "USD",', "[]", '{"rates": {"EUR": NaN}}'):
            with self.subTest(text=text):
                self.write("fx.json", text)
                errors = validate_dir(self.tmp)
                self.assertTrue(errors)
                for error in errors:
                    self.assertTrue(error.startswith("fx.json: "), error)

    def test_bad_fx_rate(self):
        rates = dict(json.loads((self.tmp / "fx.json").read_text())["rates"], EUR=-1)
        self.write("fx.json", json.dumps(dict(VALID_FX, rates=rates)))
        self.assert_single_error("fx.json", "rates.EUR must be a finite number > 0")

    def test_missing_fx_rate_is_a_warning(self):
        rates = json.loads((self.tmp / "fx.json").read_text())["rates"]
        del rates["JPY"], rates["GBp"]
        self.write("fx.json", json.dumps(dict(VALID_FX, rates=rates)))
        self.assertEqual(
            check_dir(self.tmp),
            (
                [],
                [
                    "fx.json: no rate for GBp, used by ezj.json",
                    "fx.json: no rate for JPY, used by 9201.json",
                ],
            ),
        )

    def test_missing_fx_file_is_a_warning(self):
        (self.tmp / "fx.json").unlink()
        errors, warnings = check_dir(self.tmp)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("fx.json: missing", warnings[0])

    def test_repo_fx_covers_every_stock_currency(self):
        currencies = {
            json.loads((REPO_ROOT / "data" / f"{key}.json").read_text())["currency"]
            for key in stocks
        }
        rates = json.loads((REPO_ROOT / "data" / "fx.json").read_text())["rates"]
        self.assertEqual(sorted(currencies - set(rates)), [])

    def test_wrong_type(self):
        self.write("dal.json", json.dumps(dict(VALID, marketCap=1.5)))
        self.assert_single_error("dal.json", "marketCap must be an integer")

    def test_missing_field(self):
        self.write("ual.json", json.dumps({"stockPrice": 1, "currency": "USD", "peRatio": None}))
        self.assert_single_error("ual.json", "marketCap must be an integer, got None")

    def test_nan_and_infinity_literals(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                self.write(
                    "aal.json",
                    '{"stockPrice": %s, "currency": "USD", "marketCap": 1, "peRatio": null}'
                    % literal,
                )
                self.assert_single_error("aal.json", "invalid JSON", literal)

    def test_empty_currency(self):
        self.write("ezj.json", json.dumps(dict(VALID, currency="")))
        self.assert_single_error("ezj.json", "currency must be a non-empty string")

    def test_malformed_json(self):
        self.write("luv.json", '{"stockPrice": 1,')
        self.assert_single_error("luv.json", "invalid JSON")

    def test_missing_ticker_file(self):
        (self.tmp / "wizz.json").unlink()
        self.assert_single_error("wizz.json", "missing file", "WIZZ.L")

    def test_extra_file(self):
        self.write("zzz.json", json.dumps(VALID))
        self.assert_single_error("zzz.json", "unexpected file")

    def test_missing_directory(self):
        errors = validate_dir(self.tmp / "nope")
        self.assertEqual(len(errors), 1)
        self.assertIn("does not exist", errors[0])

    def test_every_ticker_has_a_file(self):
        self.assertEqual(
            sorted(p.stem for p in (REPO_ROOT / "data").glob("*.json") if p.name != "fx.json"),
            sorted(stocks),
        )

    def test_main_exit_codes_and_messages(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--data-dir", str(self.tmp)]), 0)
        self.assertIn("OK: 25 files", out.getvalue())

        self.write("dal.json", "[]")
        (self.tmp / "qan.json").unlink()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--data-dir", str(self.tmp)]), 1)
        self.assertIn("::error::dal.json: expected a JSON object", out.getvalue())
        self.assertIn("::error::qan.json: missing file", out.getvalue())
        self.assertIn("2 data contract problem(s)", out.getvalue())

    def test_main_reports_fx_problems(self):
        self.write("fx.json", '{"base": "USD"')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--data-dir", str(self.tmp)]), 1)
        self.assertIn("::error::fx.json: invalid JSON", out.getvalue())

        self.write("fx.json", json.dumps(dict(VALID_FX, rates={"USD": 1})))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--data-dir", str(self.tmp)]), 0)
        self.assertIn("::warning::fx.json: no rate for EUR, used by airf.json", out.getvalue())
        self.assertIn("OK: 25 files", out.getvalue())


if __name__ == "__main__":
    unittest.main()
