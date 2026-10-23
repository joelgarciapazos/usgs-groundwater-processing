#!/usr/bin/env python3
"""Verify the frozen inputs required to reproduce manuscript Table A2."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUMMARY = REPO / "docs" / "interpolation_method_comparison_summary.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def readjson(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def readcsv(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


def require_hash(path: Path, expected: str, label: str) -> None:
    if not path.exists():
        raise ValueError(f"Missing {label}: {path}")
    actual = sha256(path)
    if actual != expected:
        raise ValueError(
            f"SHA-256 mismatch for {label}: expected {expected}, got {actual}"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--auditoria", type=Path, required=True)
    p.add_argument("--desarrollo", type=Path, required=True)
    args = p.parse_args()

    audit = args.auditoria.expanduser().resolve()
    dev = args.desarrollo.expanduser().resolve()
    frozen = readjson(SUMMARY)

    require_hash(audit / "resumen.json", frozen["audit_summary_sha256"], "audit resumen.json")
    require_hash(dev / "resumen.json", frozen["development_summary_sha256"], "development resumen.json")
    require_hash(
        audit / "manifiesto_ventanas_desarrollo.csv",
        frozen["development_masks_sha256"],
        "development-mask manifest",
    )
    require_hash(
        dev / "metricas_segmento_longitud.csv",
        frozen["frozen_pchip_metrics_sha256"],
        "frozen PCHIP metrics",
    )

    audit_summary = readjson(audit / "resumen.json")
    dev_summary = readjson(dev / "resumen.json")

    for name, expected in audit_summary.get("output_hashes", {}).items():
        require_hash(audit / name, expected, f"audit output {name}")

    for name, expected in dev_summary.get("output_hashes", {}).items():
        require_hash(dev / name, expected, f"development output {name}")

    if dev_summary.get("inputs", {}).get("audit_summary") != sha256(audit / "resumen.json"):
        raise ValueError("Development summary does not point to this audit summary")
    if dev_summary.get("inputs", {}).get("masks") != sha256(audit / "manifiesto_ventanas_desarrollo.csv"):
        raise ValueError("Development summary does not point to this mask manifest")

    segments = list(readcsv(audit / "segmentos_elegibles_30d.csv"))
    for row in segments:
        path = (audit / row["segment_file_v2"]).resolve()
        if not path.is_relative_to(audit):
            raise ValueError(f"Segment path escapes audit directory: {row['segment_id']}")
        require_hash(path, row["segment_sha256_v2"], f"segment {row['segment_id']}")

    masks = list(readcsv(audit / "manifiesto_ventanas_desarrollo.csv"))
    status = list(readcsv(dev / "estado_segmentos.csv"))
    metrics = list(readcsv(dev / "metricas_segmento_longitud.csv"))

    evaluated = {r["segment_id"] for r in status if r["status"] == "evaluado_desarrollo"}
    used_masks = [r for r in masks if r["segment_id"] in evaluated]

    expected_masks = int(audit_summary["n_windows_selected"])
    expected_units = int(dev_summary["n_segment_length_metrics"])
    expected_evaluated = int(dev_summary["statuses"]["evaluado_desarrollo"])

    if len(segments) != int(audit_summary["auditoria_flujo"]["n_eligible_segments_gap30"]):
        raise ValueError("Eligible-segment count does not match audit summary")
    if len(masks) != expected_masks or len(used_masks) != expected_masks:
        raise ValueError("Development-mask count does not match frozen summary")
    if len(evaluated) != expected_evaluated:
        raise ValueError("Evaluated-segment count does not match development summary")
    if len(metrics) != expected_units:
        raise ValueError("Segment-length metric count does not match development summary")

    print("TABLE A2 FROZEN INPUTS: PASS")
    print(f"Audit directory: {audit}")
    print(f"Development directory: {dev}")
    print(f"Eligible segments: {len(segments)}")
    print(f"Evaluated development segments: {len(evaluated)}")
    print(f"Frozen development masks: {len(masks)}")
    print(f"Frozen PCHIP segment-length units: {len(metrics)}")


if __name__ == "__main__":
    main()
