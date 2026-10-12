#!/usr/bin/env python3
"""Audit whether collapsed identical-value duplicate flags became continuity barriers.

This is a read-only diagnostic for the frozen workflow. It does not change QA or
continuity decisions. It reports how many prepared source dates carry the
`identical_value_duplicates_collapsed` flag, how many of those dates are marked
unusable by the continuity policy, and whether they fall inside the final
selected segment intervals.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from datetime import date
from pathlib import Path

FLAG = "identical_value_duplicates_collapsed"


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("experiment", type=Path, help="Frozen experiment directory (exp_...)")
    p.add_argument("--report", type=Path, help="Optional JSON output")
    return p


def readcsv(p: Path):
    op = gzip.open if p.suffix == ".gz" else open
    with op(p, "rt", encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


def pointer(ex: Path, folder: str, name: str) -> Path:
    p = ex / folder / name
    return Path(p.read_text(encoding="utf-8-sig").strip()).resolve()


def main():
    args = parser().parse_args()
    ex = args.experiment.expanduser().resolve()
    prepared_inventory = ex / "01_preparados" / "inventario_preparado.csv"
    if not prepared_inventory.is_file():
        raise FileNotFoundError(f"Missing prepared inventory: {prepared_inventory}")
    sel = pointer(ex, "07_seleccion_pre_reserva_v2", "ultima_seleccion.txt")
    selected = list(readcsv(sel / "segmentos_seleccionados_pre_reserva.csv"))
    intervals = {
        r["monitoring_location_id"]: (date.fromisoformat(r["date_min"]), date.fromisoformat(r["date_max"]), r["segment_id"])
        for r in selected
    }

    flagged_dates = barrier_dates = selected_flagged = selected_barriers = 0
    flagged_wells, barrier_wells, selected_flagged_wells, selected_barrier_wells = set(), set(), set(), set()
    examples = []
    for inv in readcsv(prepared_inventory):
        rel = inv.get("prepared_file", "")
        if not rel:
            continue
        p = (ex / rel).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Prepared series missing: {p}")
        mid = inv["monitoring_location_id"]
        interval = intervals.get(mid)
        for r in readcsv(p):
            flags = set(filter(None, r.get("qa_flags", "").split("|")))
            if FLAG not in flags:
                continue
            flagged_dates += 1; flagged_wells.add(mid)
            unusable = str(r.get("usable_input", "")).lower() in {"0", "false"}
            if unusable:
                barrier_dates += 1; barrier_wells.add(mid)
            inside = False
            if interval:
                d = date.fromisoformat(r["date"])
                inside = interval[0] <= d <= interval[1]
                if inside:
                    selected_flagged += 1; selected_flagged_wells.add(mid)
                    if unusable:
                        selected_barriers += 1; selected_barrier_wells.add(mid)
            if len(examples) < 20:
                examples.append({"monitoring_location_id": mid, "date": r["date"],
                                 "usable_input": r.get("usable_input"),
                                 "barrier_reason": r.get("barrier_reason"),
                                 "inside_selected_interval": inside})

    report = {
        "flag": FLAG,
        "flagged_dates": flagged_dates,
        "flagged_wells": len(flagged_wells),
        "flagged_dates_marked_unusable": barrier_dates,
        "wells_with_flagged_unusable_dates": len(barrier_wells),
        "flagged_dates_inside_selected_intervals": selected_flagged,
        "selected_wells_with_flagged_dates": len(selected_flagged_wells),
        "flagged_unusable_dates_inside_selected_intervals": selected_barriers,
        "selected_wells_with_flagged_unusable_dates": len(selected_barrier_wells),
        "examples": examples,
        "interpretation": (
            "Read-only diagnostic. A nonzero count does not by itself imply an error; "
            "it identifies where the historical continuity policy treated this QA flag as a barrier."
        ),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
