#!/usr/bin/env python3
"""Fetch airline stock data from Yahoo Finance into data/<key>.json.

Each file follows a four-field JSON contract that external tools rely on:

  stockPrice  number
  currency    non-empty string, for example USD or GBp
  marketCap   integer
  peRatio     number, or null when unavailable or non-finite

Every symbol is tried MAX_ATTEMPTS times with a growing delay. When all
attempts fail, the existing file is kept if it is still valid. The script
exits non-zero only when a symbol fails and has no valid existing file, after
every other symbol has been processed.

After the stocks, it writes data/fx.json with USD exchange rates for every
currency the stock files use, also from Yahoo Finance:

  {
    "base": "USD",
    "source": "Yahoo Finance",
    "updatedAt": "2026-10-26T13:30:00Z",
    "rates": {"EUR": 1.08, "GBp": 0.0126, "USD": 1}
  }

rates[c] is the number of USD per one unit of currency c, so a value v in c
is v * rates[c] USD. USD is always 1. GBp (London listings quoted in pence) is
the GBP rate / 100. Each rate is the latest daily close of <c>USD=X (the
live quote while the day is open), fetched with the same retries;
when they all fail the last valid rate in the existing file is kept, and a
currency with no rate at all is left out with a warning. When no stock quote
could be fetched in the run, Yahoo is treated as down and the FX fetches are
skipped, leaving fx.json as it is. All FX fetching also stops after
FX_TIME_BUDGET_SECONDS, with each request's timeout capped by the time left,
and the currencies not reached keep their last rate, so FX retries cannot push
the run past the workflow's timeout. FX problems never make the script exit
non-zero. updatedAt is the UTC time of the run that last
changed a rate, so runs where no rate changed leave the file untouched.

Usage:
  python scripts/update_data.py [--data-dir DIR]

Needs yfinance at run time. It is imported lazily, so the rest of this module
(and scripts/validate_data.py, which imports it) works with the standard
library only.
"""

import argparse
import json
import math
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FX_FILE = "fx.json"
FX_BASE = "USD"
FX_SOURCE = "Yahoo Finance"
# Currencies quoted in a sub-unit: derived from the main unit, not fetched.
FX_SUBUNITS = {"GBp": ("GBP", 100)}
FX_SIGNIFICANT_DIGITS = 6
# Total time for FX fetches, retries and pauses. A normal run needs ~30 s.
FX_TIME_BUDGET_SECONDS = 120
FX_REQUEST_TIMEOUT_SECONDS = 10

stocks = {
    "alk": "ALK",
    "dal": "DAL",
    "9201": "9201.T",
    "lha": "LHA.DE",
    "qan": "QAN.AX",
    "ual": "UAL",
    "aal": "AAL",
    "ac": "AC.TO",
    "airf": "AF.PA",
    "sial": "C6L.SI",
    "2618": "2618.TW",
    "0753": "0753.HK",
    "2610": "2610.TW",
    "600115": "600115.SS",
    "600029": "600029.SS",
    "cpa": "CPA",
    "ltm": "LTM",
    "thai": "THAI.BK",
    "algt": "ALGT",
    "ezj": "EZJ.L",
    "jblu": "JBLU",
    "nas": "NAS.OL",
    "rya": "RYA.IR",
    "luv": "LUV",
    "wizz": "WIZZ.L",
}

MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 5


def clean_number(value, decimals=None):
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    if decimals is None:
        return int(value)
    return round(value, decimals)


def safe_get(mapping, key):
    try:
        return mapping.get(key)
    except Exception:
        return None


def first_valid_number(*values):
    for value in values:
        try:
            cleaned = clean_number(value, 8)
        except (TypeError, ValueError, OverflowError):
            continue
        if cleaned is not None:
            return value
    return None


def _is_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def validate_record(record):
    """Return a list of contract violations; an empty list means valid."""
    if not isinstance(record, dict):
        return [f"expected a JSON object, got {type(record).__name__}"]
    problems = []
    if not _is_number(record.get("stockPrice")):
        problems.append(
            f"stockPrice must be a finite number, got {record.get('stockPrice')!r}"
        )
    currency = record.get("currency")
    if not isinstance(currency, str) or not currency:
        problems.append(f"currency must be a non-empty string, got {currency!r}")
    market_cap = record.get("marketCap")
    if not isinstance(market_cap, int) or isinstance(market_cap, bool):
        problems.append(f"marketCap must be an integer, got {market_cap!r}")
    if "peRatio" not in record:
        problems.append("peRatio is missing (use null when unavailable)")
    elif record["peRatio"] is not None and not _is_number(record["peRatio"]):
        problems.append(
            f"peRatio must be a finite number or null, got {record['peRatio']!r}"
        )
    return problems


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant {name}")


def read_record(path):
    """Parse a data file, rejecting NaN and Infinity literals."""
    return json.loads(path.read_text(), parse_constant=_reject_constant)


def load_existing(path):
    if not path.exists():
        return None
    try:
        existing = read_record(path)
    except (OSError, ValueError):
        return None
    if validate_record(existing):
        return None
    return existing


def validate_fx_record(record):
    """Return a list of fx.json shape violations; an empty list means valid."""
    if not isinstance(record, dict):
        return [f"expected a JSON object, got {type(record).__name__}"]
    problems = []
    if record.get("base") != FX_BASE:
        problems.append(f"base must be {FX_BASE!r}, got {record.get('base')!r}")
    source = record.get("source")
    if not isinstance(source, str) or not source:
        problems.append(f"source must be a non-empty string, got {source!r}")
    updated_at = record.get("updatedAt")
    try:
        datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        problems.append(
            f"updatedAt must be a UTC time like 2026-10-26T13:30:00Z, got {updated_at!r}"
        )
    rates = record.get("rates")
    if not isinstance(rates, dict):
        problems.append(f"rates must be a JSON object, got {rates!r}")
        return problems
    for currency, rate in rates.items():
        if not currency:
            problems.append("rates has an empty currency code")
        elif not _is_number(rate) or rate <= 0:
            problems.append(
                f"rates.{currency} must be a finite number > 0, got {rate!r}"
            )
    if FX_BASE in rates and rates[FX_BASE] != 1:
        problems.append(f"rates.{FX_BASE} must be 1, got {rates[FX_BASE]!r}")
    return problems


def default_ticker_factory(symbol):
    import yfinance as yf

    return yf.Ticker(symbol)


def fetch_symbol(symbol, ticker_factory=default_ticker_factory):
    ticker = ticker_factory(symbol)

    fast = {}
    try:
        fast = ticker.fast_info
    except Exception as exc:
        print(f"::warning::{symbol}: fast_info failed: {exc}")

    info = {}
    try:
        info = ticker.get_info() or {}
    except Exception as exc:
        print(f"::warning::{symbol}: get_info failed: {exc}")

    history_price = None
    try:
        history = ticker.history(period="5d", interval="1d", auto_adjust=False)
        if not history.empty:
            closes = history["Close"].dropna()
            if not closes.empty:
                history_price = closes.iloc[-1]
    except Exception as exc:
        print(f"::warning::{symbol}: history fallback failed: {exc}")

    price = first_valid_number(
        safe_get(fast, "last_price"),
        info.get("currentPrice"),
        info.get("regularMarketPrice"),
        info.get("previousClose"),
        history_price,
    )
    market_cap = first_valid_number(
        safe_get(fast, "market_cap"),
        info.get("marketCap"),
    )
    pe_ratio = first_valid_number(
        info.get("trailingPE"),
        info.get("forwardPE"),
    )
    currency = info.get("currency") or safe_get(fast, "currency")

    missing = [
        name
        for name, value in {
            "stockPrice": price,
            "marketCap": market_cap,
            "currency": currency,
        }.items()
        if value is None or value == ""
    ]
    if missing:
        raise RuntimeError(f"missing required fields: {', '.join(missing)}")

    return {
        "stockPrice": clean_number(price, 2),
        "currency": str(currency),
        "marketCap": clean_number(market_cap),
        "peRatio": clean_number(pe_ratio, 2),
    }


def fx_symbol(currency):
    """Yahoo symbol quoting USD per one unit of currency."""
    return f"{currency}{FX_BASE}=X"


def round_rate(rate):
    return float(f"{rate:.{FX_SIGNIFICANT_DIGITS}g}")


def fetch_fx_rate(
    symbol, ticker_factory=default_ticker_factory, timeout=FX_REQUEST_TIMEOUT_SECONDS
):
    """Return the latest close of an FX symbol as a finite number > 0.

    One request, so timeout bounds how long it can take.
    """
    history = ticker_factory(symbol).history(
        period="5d", interval="1d", auto_adjust=False, timeout=timeout
    )
    closes = history["Close"].dropna() if not history.empty else None
    if closes is not None and not closes.empty:
        try:
            rate = float(closes.iloc[-1])
        except (TypeError, ValueError, OverflowError):
            rate = None
        if rate is not None and math.isfinite(rate) and rate > 0:
            return rate
    raise RuntimeError("no valid FX quote")


def load_existing_fx(path):
    """Return the existing fx.json record if it is valid, else None."""
    if not path.exists():
        return None
    try:
        existing = read_record(path)
    except (OSError, ValueError):
        return None
    if validate_fx_record(existing):
        return None
    return existing


def stock_currencies(data_dir, symbols):
    """Distinct currencies of the valid stock files for symbols."""
    currencies = set()
    for file_key in symbols:
        record = load_existing(data_dir / f"{file_key}.json")
        if record is not None:
            currencies.add(record["currency"])
    return currencies


def update_fx(data_dir, currencies, ticker_factory, sleep, now, clock):
    """Write data_dir/fx.json with a USD rate for each currency.

    Failures are warnings: a currency keeps its last valid rate, or is left
    out when it has none. Fetching stops once FX_TIME_BUDGET_SECONDS have
    passed on clock. Returns the rates that were written or kept.
    """
    path = data_dir / FX_FILE
    existing = load_existing_fx(path)
    old_rates = existing["rates"] if existing else {}

    to_fetch = sorted(
        {FX_SUBUNITS.get(c, (c, 1))[0] for c in currencies} - {FX_BASE}
    )
    fetched = {}
    deadline = clock() + FX_TIME_BUDGET_SECONDS
    for index, currency in enumerate(to_fetch, start=1):
        if clock() >= deadline:
            break
        symbol = fx_symbol(currency)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - clock()
            if remaining <= 0:
                break
            try:
                fetched[currency] = fetch_fx_rate(
                    symbol,
                    ticker_factory,
                    min(FX_REQUEST_TIMEOUT_SECONDS, remaining),
                )
                print(f"{symbol}: {fetched[currency]}")
                break
            except Exception as exc:
                print(
                    f"::warning::{symbol}: attempt {attempt}/{MAX_ATTEMPTS} "
                    f"failed: {type(exc).__name__}: {exc}"
                )
                delay = BASE_DELAY_SECONDS * attempt
                if attempt == MAX_ATTEMPTS or clock() + delay >= deadline:
                    break
                sleep(delay)
        if index < len(to_fetch):
            sleep(1)
    missed = [c for c in to_fetch if c not in fetched]
    if missed and clock() >= deadline:
        print(
            f"::warning::FX time budget of {FX_TIME_BUDGET_SECONDS}s used up "
            f"with no new rate for {', '.join(missed)}"
        )

    rates = {FX_BASE: 1}
    for currency in sorted(currencies - {FX_BASE}):
        parent, divisor = FX_SUBUNITS.get(currency, (currency, 1))
        if parent in fetched:
            rates[currency] = round_rate(fetched[parent] / divisor)
        elif currency in old_rates:
            rates[currency] = old_rates[currency]
            print(
                f"::warning::{fx_symbol(parent)}: no new rate; "
                f"keeping the last {currency} rate {old_rates[currency]}"
            )
        else:
            print(
                f"::warning::{fx_symbol(parent)}: no new rate and "
                f"no earlier one; leaving {currency} out of {path}"
            )
    rates = dict(sorted(rates.items()))

    if existing is not None and existing["rates"] == rates and (
        existing["source"] == FX_SOURCE
    ):
        print(f"FX rates unchanged; leaving {path} as is")
        return rates

    record = {
        "base": FX_BASE,
        "source": FX_SOURCE,
        "updatedAt": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rates": rates,
    }
    path.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(f"Wrote {len(rates)} FX rates to {path}")
    return rates


def utc_now():
    return datetime.now(timezone.utc)


def run(
    data_dir=DEFAULT_DATA_DIR,
    ticker_factory=default_ticker_factory,
    sleep=time.sleep,
    symbols=None,
    now=utc_now,
    clock=time.monotonic,
):
    """Update every symbol, then fx.json, and return the exit code (0 or 1).

    symbols maps file keys to Yahoo symbols and defaults to `stocks`.
    """
    if symbols is None:
        symbols = stocks
    data_dir = Path(data_dir)
    data_dir.mkdir(exist_ok=True)
    unrecoverable = []
    fetched_any = False

    for index, (file_key, symbol) in enumerate(symbols.items(), start=1):
        path = data_dir / f"{file_key}.json"
        existing = load_existing(path)
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                out = fetch_symbol(symbol, ticker_factory)
                path.write_text(
                    json.dumps(out, indent=2, allow_nan=False) + "\n"
                )
                print(
                    f"{symbol}: {out['stockPrice']} {out['currency']} | "
                    f"market cap {out['marketCap']} | P/E {out['peRatio']}"
                )
                last_error = None
                fetched_any = True
                break
            except Exception as exc:
                last_error = exc
                print(
                    f"::warning::{symbol}: attempt {attempt}/{MAX_ATTEMPTS} "
                    f"failed: {type(exc).__name__}: {exc}"
                )
                if attempt < MAX_ATTEMPTS:
                    sleep(BASE_DELAY_SECONDS * attempt)
        else:
            if existing is not None:
                print(
                    f"::warning::{symbol}: all attempts failed; "
                    f"keeping existing {path}"
                )
            else:
                unrecoverable.append(f"{symbol}: {last_error}")
                print(f"::error::{symbol}: no valid existing data is available")
                traceback.print_exception(last_error)

        if index < len(symbols):
            sleep(1)

    if not fetched_any:
        print(
            "::warning::no stock quote was fetched; skipping FX fetches and "
            f"leaving {data_dir / FX_FILE} as is"
        )
    else:
        try:
            update_fx(
                data_dir,
                stock_currencies(data_dir, symbols),
                ticker_factory,
                sleep,
                now,
                clock,
            )
        except Exception as exc:
            print(
                f"::warning::FX rates were not updated: {type(exc).__name__}: {exc}"
            )
            traceback.print_exception(exc)

    if unrecoverable:
        print(
            "Unable to update symbols without valid fallback data: "
            + "; ".join(unrecoverable),
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="directory holding <key>.json files (default: the repo's data/)",
    )
    args = parser.parse_args(argv)
    return run(data_dir=args.data_dir)


if __name__ == "__main__":
    sys.exit(main())
