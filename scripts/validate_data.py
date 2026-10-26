#!/usr/bin/env python3
"""Check that data/*.json still matches the four-field JSON contract.

Every file must parse as strict JSON (no NaN or Infinity literals) and pass
validate_record from update_data.py, the same check the updater uses to decide
whether an existing file is a valid fallback. The set of files must also match
the ticker list in update_data.py exactly: no missing and no unexpected files,
apart from fx.json.

fx.json, when present, must be strict JSON with the shape checked by
validate_fx_record: base USD, a source, an updatedAt UTC time and rates of
finite numbers > 0 (USD per unit). A malformed fx.json is an error. A missing
fx.json, or a currency used by a stock file with no rate, is only a warning:
the updater leaves a currency out when Yahoo has no rate for it, and that must
not block committing stock data.

Exits non-zero and prints one message per problem; warnings alone exit zero.

Usage:
  python scripts/validate_data.py [--data-dir DIR]

Standard library only.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from update_data import (  # noqa: E402
    DEFAULT_DATA_DIR,
    FX_FILE,
    read_record,
    stocks,
    validate_fx_record,
    validate_record,
)


def check_dir(data_dir, symbols=None):
    """Return (errors, warnings), lists of "<file>: <problem>" messages."""
    if symbols is None:
        symbols = stocks
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return [f"{data_dir}: data directory does not exist"], []

    expected = {f"{key}.json" for key in symbols}
    present = {path.name for path in data_dir.glob("*.json")}
    errors = []
    warnings = []
    currencies = {}

    for name in sorted(expected - present):
        errors.append(f"{name}: missing file for ticker {symbols[name[:-5]]}")
    for name in sorted(present - expected - {FX_FILE}):
        errors.append(f"{name}: unexpected file, not in the ticker list")

    for name in sorted(expected & present):
        try:
            record = read_record(data_dir / name)
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: invalid JSON: {exc}")
            continue
        problems = validate_record(record)
        errors.extend(f"{name}: {problem}" for problem in problems)
        if not problems:
            currencies.setdefault(record["currency"], name)

    if FX_FILE not in present:
        warnings.append(f"{FX_FILE}: missing, so no USD rates are published")
        return errors, warnings
    try:
        fx = read_record(data_dir / FX_FILE)
    except (OSError, ValueError) as exc:
        errors.append(f"{FX_FILE}: invalid JSON: {exc}")
        return errors, warnings
    problems = validate_fx_record(fx)
    errors.extend(f"{FX_FILE}: {problem}" for problem in problems)
    if not problems:
        for currency in sorted(set(currencies) - set(fx["rates"])):
            warnings.append(
                f"{FX_FILE}: no rate for {currency}, used by {currencies[currency]}"
            )

    return errors, warnings


def validate_dir(data_dir, symbols=None):
    """Return a list of "<file>: <problem>" errors; empty means valid."""
    return check_dir(data_dir, symbols)[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="directory holding <key>.json files (default: the repo's data/)",
    )
    args = parser.parse_args(argv)

    errors, warnings = check_dir(args.data_dir)
    for warning in warnings:
        print(f"::warning::{warning}")
    if errors:
        for error in errors:
            print(f"::error::{error}")
        print(f"{len(errors)} data contract problem(s) in {args.data_dir}")
        return 1
    print(f"OK: {len(stocks)} files in {args.data_dir} match the data contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
