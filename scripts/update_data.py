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
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

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


def run(
    data_dir=DEFAULT_DATA_DIR,
    ticker_factory=default_ticker_factory,
    sleep=time.sleep,
    symbols=None,
):
    """Update every symbol and return the process exit code (0 or 1).

    symbols maps file keys to Yahoo symbols and defaults to `stocks`.
    """
    if symbols is None:
        symbols = stocks
    data_dir = Path(data_dir)
    data_dir.mkdir(exist_ok=True)
    unrecoverable = []

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
