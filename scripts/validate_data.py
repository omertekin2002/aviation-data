#!/usr/bin/env python3
"""Check that data/*.json still matches the four-field JSON contract.

Every file must parse as strict JSON (no NaN or Infinity literals) and pass
validate_record from update_data.py, the same check the updater uses to decide
whether an existing file is a valid fallback. The set of files must also match
the ticker list in update_data.py exactly: no missing and no unexpected files.

Exits non-zero and prints one message per problem file.

Usage:
  python scripts/validate_data.py [--data-dir DIR]

Standard library only.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from update_data import DEFAULT_DATA_DIR, read_record, stocks, validate_record  # noqa: E402


def validate_dir(data_dir, symbols=None):
    """Return a list of "<file>: <problem>" messages; empty means valid."""
    if symbols is None:
        symbols = stocks
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return [f"{data_dir}: data directory does not exist"]

    expected = {f"{key}.json" for key in symbols}
    present = {path.name for path in data_dir.glob("*.json")}
    errors = []

    for name in sorted(expected - present):
        errors.append(f"{name}: missing file for ticker {symbols[name[:-5]]}")
    for name in sorted(present - expected):
        errors.append(f"{name}: unexpected file, not in the ticker list")

    for name in sorted(expected & present):
        try:
            record = read_record(data_dir / name)
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: invalid JSON: {exc}")
            continue
        errors.extend(f"{name}: {problem}" for problem in validate_record(record))

    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="directory holding <key>.json files (default: the repo's data/)",
    )
    args = parser.parse_args(argv)

    errors = validate_dir(args.data_dir)
    if errors:
        for error in errors:
            print(f"::error::{error}")
        print(f"{len(errors)} data contract problem(s) in {args.data_dir}")
        return 1
    print(f"OK: {len(stocks)} files in {args.data_dir} match the data contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
