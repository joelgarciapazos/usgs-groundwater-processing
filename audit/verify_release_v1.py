#!/usr/bin/env python3
"""Verify a final dataset summary against the frozen Version 1.0 release counts.

This utility is intentionally separate from the scientific pipeline. A future rerun may
produce different counts if USGS revises historical source records.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

KEYS = {
    "final_wells": "n_final_wells",
    "wells_interpolated": "n_final_with_interpolation",
    "wells_without_interpolation": "n_final_without_interpolation",
    "wells_excluded_after_reserved_validation": "excluded_after_reserve",
    "source_values_retained": "n_source_values_retained",
    "interpolated_values_created": "n_interpolated_values_created",
    "final_daily_values": "n_final_daily_values",
    "natural_gaps_interpolated": "n_real_gaps_interpolated",
    "maximum_natural_gap_days": "max_real_gap_days",
}

def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))

def main():
    repo = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("summary", type=Path, help="Path to resumen_dataset_final.json")
    p.add_argument("--expected", type=Path, default=repo / "config" / "release_v1_0_expected.json")
    args = p.parse_args()
    actual = load(args.summary)
    expected = load(args.expected)
    failures = []
    for expected_key, actual_key in KEYS.items():
        ev = expected[expected_key]
        av = actual.get(actual_key)
        ok = av == ev
        print(f"{'PASS' if ok else 'FAIL'}  {actual_key}: actual={av!r} expected={ev!r}")
        if not ok:
            failures.append(actual_key)
    if failures:
        raise SystemExit(f"Release audit failed for {len(failures)} field(s): {', '.join(failures)}")
    print("Release Version 1.0 summary matches the frozen audit reference.")

if __name__ == "__main__":
    main()
