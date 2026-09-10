#!/usr/bin/env python3
"""Validate AuraPi locale files against English master locale."""
from __future__ import annotations

import json
import string
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOCALES_DIR = BASE_DIR / "locales"
MASTER = "en"


def flatten(data, prefix=""):
    result = {}
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(flatten(value, full))
        else:
            result[full] = value
    return result


def placeholders(value):
    if not isinstance(value, str):
        return []
    formatter = string.Formatter()
    fields = []
    for _, field, _, _ in formatter.parse(value):
        if field is not None:
            fields.append(field.split(".")[0].split("[")[0])
    return sorted(fields)


def load(path):
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("locale root must be a JSON object")
    return data


def main():
    master_path = LOCALES_DIR / f"{MASTER}.json"
    if not master_path.is_file():
        print(f"ERROR: missing master locale: {master_path}")
        return 1

    master = flatten(load(master_path))
    failed = False

    for path in sorted(LOCALES_DIR.glob("*.json")):
        current = flatten(load(path))
        missing = sorted(set(master) - set(current))
        extra = sorted(set(current) - set(master))
        mismatch = sorted(
            key for key in set(master) & set(current)
            if placeholders(master[key]) != placeholders(current[key])
        )

        status = "PASS" if not (missing or extra or mismatch) else "FAIL"
        print(f"{path.name}: {status} ({len(current)} keys)")
        if missing:
            failed = True
            print("  Missing:", ", ".join(missing))
        if extra:
            failed = True
            print("  Extra:", ", ".join(extra))
        if mismatch:
            failed = True
            for key in mismatch:
                print(
                    f"  Placeholder mismatch {key}: "
                    f"master={placeholders(master[key])}, "
                    f"locale={placeholders(current[key])}"
                )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
