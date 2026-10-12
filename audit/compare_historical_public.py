#!/usr/bin/env python3
"""Semantic equivalence audit for the frozen historical and public workflows.

The public repository intentionally changes filenames, paths, documentation, and
summary-closing code. Therefore byte-for-byte comparison of every output is not
appropriate. This audit compares the scientific products that must remain
identical (or numerically equal within a strict tolerance): temporal segments,
mask manifests, validation decisions/metrics, reserved decisions, final gap
reconstruction metadata, and the final daily dataset.

It never modifies either run.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from pathlib import Path
from typing import Iterable

POINTERS = {
    "audit": ("05_validacion_segmentos_v2", "ultima_auditoria.txt"),
    "development": ("06_desarrollo_todos_segmentos_v2", "ultimo_desarrollo.txt"),
    "selection": ("07_seleccion_pre_reserva_v2", "ultima_seleccion.txt"),
    "reserve": ("08_validacion_reserva_final_v2", "ultima_reserva.txt"),
    "dataset": ("09_dataset_final_v2", "ultimo_dataset.txt"),
}

TABLES = [
    ("audit", "segmentos_elegibles_30d.csv"),
    ("audit", "manifiesto_ventanas_desarrollo.csv"),
    ("audit", "auditoria_ventanas.csv"),
    ("development", "estado_segmentos.csv"),
    ("development", "metricas_segmento_longitud.csv"),
    ("development", "sensibilidad_presupuesto.csv"),
    ("selection", "decisiones_segmentos_por_pozo.csv"),
    ("selection", "segmentos_seleccionados_pre_reserva.csv"),
    ("selection", "manifiesto_ventanas_reserva_max.csv"),
    ("reserve", "estado_final_pre_interpolacion.csv"),
    ("reserve", "metricas_reserva_segmento_longitud.csv"),
    ("dataset", "metadata_pozos_finales.csv"),
    ("dataset", "huecos_interpolados.csv"),
    ("dataset", "pozos_excluidos_tras_reserva.csv"),
]

# These fields are implementation/provenance details, not scientific outputs.
IGNORE_FIELDS = {
    "segment_file_v2", "segment_sha256_v2", "series_file", "series_sha256",
    "source_file", "source_sha256", "prepared_file", "prepared_sha256",
    "script_sha256", "finalizado_utc", "inicio_utc", "created_utc", "updated_utc",
}

# Stable keys used where available. The fallback is the complete comparable row.
KEY_CANDIDATES = [
    ("monitoring_location_id", "segment_id", "gap_length_days", "mask_id"),
    ("monitoring_location_id", "segment_id", "gap_length_days"),
    ("monitoring_location_id", "segment_id"),
    ("monitoring_location_id", "gap_length_days", "budget_masks"),
    ("monitoring_location_id", "gap_id"),
    ("monitoring_location_id",),
    ("site_no", "date"),
]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("historical_experiment", type=Path,
                   help="Frozen historical experiment directory (exp_...)")
    p.add_argument("public_experiment", type=Path,
                   help="Public-workflow experiment directory produced from the same frozen inputs")
    p.add_argument("--atol", type=float, default=1e-12,
                   help="Absolute tolerance for numeric CSV values (default: 1e-12)")
    p.add_argument("--report", type=Path,
                   help="Optional JSON report path")
    return p


def read_pointer(ex: Path, stage: str) -> Path:
    folder, name = POINTERS[stage]
    p = ex / folder / name
    if not p.is_file():
        raise FileNotFoundError(f"Missing pointer: {p}")
    target = Path(p.read_text(encoding="utf-8-sig").strip()).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"Pointer target does not exist: {target}")
    return target


def opener(path: Path):
    return gzip.open if path.suffix == ".gz" else open


def rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with opener(path)(path, "rt", encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        fields = rd.fieldnames or []
        return fields, [dict(r) for r in rd]


def as_number(x: str):
    if x is None:
        return None
    s = str(x).strip()
    if not s:
        return None
    try:
        y = float(s)
    except ValueError:
        return None
    return y if math.isfinite(y) else None


def equal_cell(a: str, b: str, atol: float) -> bool:
    if a == b:
        return True
    na, nb = as_number(a), as_number(b)
    if na is None or nb is None:
        return False
    return math.isclose(na, nb, rel_tol=0.0, abs_tol=atol)


def choose_key(fields: Iterable[str]) -> tuple[str, ...]:
    fs = set(fields)
    for key in KEY_CANDIDATES:
        if set(key).issubset(fs):
            return key
    return tuple()


def compare_table(a: Path, b: Path, atol: float) -> dict:
    af, ar = rows(a)
    bf, br = rows(b)
    comparable = [x for x in af if x in bf and x not in IGNORE_FIELDS]
    missing_a = sorted(set(bf) - set(af) - IGNORE_FIELDS)
    missing_b = sorted(set(af) - set(bf) - IGNORE_FIELDS)
    key = choose_key(comparable)

    def index(rs):
        out = {}
        for i, r in enumerate(rs):
            k = tuple(r.get(x, "") for x in key) if key else tuple(r.get(x, "") for x in comparable)
            if k in out:
                k = k + (f"__row_{i}",)
            out[k] = r
        return out

    ai, bi = index(ar), index(br)
    only_a = sorted(set(ai) - set(bi), key=str)
    only_b = sorted(set(bi) - set(ai), key=str)
    diffs = []
    for k in sorted(set(ai) & set(bi), key=str):
        for field in comparable:
            va, vb = ai[k].get(field, ""), bi[k].get(field, "")
            if not equal_cell(va, vb, atol):
                diffs.append({"key": k, "field": field, "historical": va, "public": vb})
                if len(diffs) >= 25:
                    break
        if len(diffs) >= 25:
            break
    ok = not missing_a and not missing_b and not only_a and not only_b and not diffs and len(ar) == len(br)
    return {
        "ok": ok,
        "historical_rows": len(ar), "public_rows": len(br),
        "key_fields": list(key),
        "missing_fields_in_historical": missing_a,
        "missing_fields_in_public": missing_b,
        "historical_only_keys_count": len(only_a),
        "public_only_keys_count": len(only_b),
        "historical_only_key_examples": only_a[:10],
        "public_only_key_examples": only_b[:10],
        "value_difference_examples": diffs,
    }


def stream_final_daily(a: Path, b: Path, atol: float) -> dict:
    required = ["site_no", "date", "groundwater_depth_m", "latitude", "longitude",
                "observed_flag", "interpolated_flag"]
    mismatch = []
    count = 0
    with gzip.open(a, "rt", encoding="utf-8-sig", newline="") as fa, \
         gzip.open(b, "rt", encoding="utf-8-sig", newline="") as fb:
        ra, rb = csv.DictReader(fa), csv.DictReader(fb)
        for f in required:
            if f not in (ra.fieldnames or []) or f not in (rb.fieldnames or []):
                return {"ok": False, "error": f"Missing required final-dataset field: {f}"}
        while True:
            xa, xb = next(ra, None), next(rb, None)
            if xa is None or xb is None:
                if xa is not None or xb is not None:
                    mismatch.append({"row": count + 1, "reason": "different row counts"})
                break
            count += 1
            for f in required:
                if not equal_cell(xa[f], xb[f], atol):
                    mismatch.append({"row": count, "field": f,
                                     "historical": xa[f], "public": xb[f],
                                     "site_no_historical": xa.get("site_no"),
                                     "date_historical": xa.get("date")})
                    break
            if len(mismatch) >= 25:
                break
    return {"ok": not mismatch, "rows_compared": count,
            "difference_examples": mismatch}


def main() -> int:
    args = parser().parse_args()
    h_ex = args.historical_experiment.expanduser().resolve()
    p_ex = args.public_experiment.expanduser().resolve()
    if h_ex == p_ex:
        raise ValueError("Historical and public experiment paths must be different")

    hs = {s: read_pointer(h_ex, s) for s in POINTERS}
    ps = {s: read_pointer(p_ex, s) for s in POINTERS}
    report = {"historical_experiment": str(h_ex), "public_experiment": str(p_ex),
              "absolute_tolerance": args.atol, "tables": {}, "final_daily": {}}
    all_ok = True

    for stage, filename in TABLES:
        ha, pa = hs[stage] / filename, ps[stage] / filename
        name = f"{stage}/{filename}"
        if not ha.is_file() or not pa.is_file():
            result = {"ok": False, "error": f"Missing comparison file: {ha if not ha.is_file() else pa}"}
        else:
            result = compare_table(ha, pa, args.atol)
        report["tables"][name] = result
        all_ok &= bool(result.get("ok"))
        print(f"{'PASS' if result.get('ok') else 'FAIL'}  {name}")

    final_name = "dataset_groundwater_daily_final.csv.gz"
    ha, pa = hs["dataset"] / final_name, ps["dataset"] / final_name
    if ha.is_file() and pa.is_file():
        result = stream_final_daily(ha, pa, args.atol)
    else:
        result = {"ok": False, "error": "Missing final combined dataset in one or both runs"}
    report["final_daily"] = result
    all_ok &= bool(result.get("ok"))
    print(f"{'PASS' if result.get('ok') else 'FAIL'}  dataset/{final_name}")

    report["equivalent"] = all_ok
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=list) + "\n",
                               encoding="utf-8")
        print(f"Report: {args.report}")
    print("SCIENTIFIC EQUIVALENCE:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
