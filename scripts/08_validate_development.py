#!/usr/bin/env python3
"""Evaluate the frozen development masks with PCHIP and summarize error and mask-budget sensitivity without opening the reserve."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import platform
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import scipy
from scipy.interpolate import PchipInterpolator

from continuity_common import DEFAULT_QA, boolean, now, readcsv, readjson, sha, writecsv, writejson

VERSION = "2.0.0"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "validation.json"
BASE_LENGTHS = (1, 3, 7, 15, 30)
BUDGETS = (10, 20, 30, 50)

MASK_METRIC_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id", "gap_length_days",
    "mask_id", "start_date", "end_date", "year", "quarter", "segment_zone",
    "n_test", "sse_m2", "sae_m", "sum_error_m", "rmse_m", "mae_m", "bias_m",
    "p95_abs_error_m",
]
DAY_FIELDS = [
    "monitoring_location_id", "segment_id", "gap_length_days", "mask_id", "date",
    "observed_m", "predicted_m", "error_m", "abs_error_m",
]
SEGMENT_METRIC_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id", "gap_length_days",
    "n_masks", "n_days_evaluated", "rmse_m", "mae_m", "bias_m", "p95_abs_error_m",
    "rmse_le_0_20", "fraction_to_interpolate", "max_gap_missing_days",
]
SENS_FIELDS = [
    "monitoring_location_id", "segment_id", "gap_length_days", "budget_masks",
    "n_full_masks", "n_resamples", "full_rmse_m", "full_mae_m",
    "q95_abs_delta_rmse_m", "q95_abs_delta_mae_m", "mean_rmse_budget_m",
    "mean_mae_budget_m", "decision_flip_fraction_vs_full", "stability_le_0_02_rmse",
    "stability_le_0_02_mae", "budget_evaluable", "has_larger_reference_panel",
]


def phase(mid: str, year: int) -> str:
    offset = int(hashlib.sha256(mid.encode()).hexdigest()[:8], 16) % 5
    return "reservada" if year % 5 == offset else "desarrollo"


def stable_rng(seed: int, *parts: object):
    h = hashlib.sha256("|".join(map(str, (seed,) + parts)).encode()).hexdigest()
    return np.random.default_rng(int(h[:16], 16))



def relevant_lengths(gmax: int) -> list[int]:
    if gmax <= 0:
        return []
    if gmax > 30:
        raise ValueError(f"Gmax fuera del escenario 30 d: {gmax}")
    return sorted(set([x for x in BASE_LENGTHS if x <= gmax] + [gmax]))


def configuration(path: Path) -> dict:
    c = readjson(path)
    if c.get("gap_scenario_days") != 30:
        raise ValueError("gap_scenario_days debe permanecer en 30")
    if c.get("min_years") != 10:
        raise ValueError("min_years debe permanecer en 10")
    if c.get("seed") != 42:
        raise ValueError("seed debe permanecer en 42")
    if int(c.get("guard_days", 0)) != 3:
        raise ValueError("guard_days debe permanecer exactamente en 3")
    if int(c.get("max_masks_per_length", 0)) != 50:
        raise ValueError("max_masks_per_length debe permanecer exactamente en 50")
    if list(c.get("budget_sensitivity", [])) != [10, 20, 30, 50]:
        raise ValueError("budget_sensitivity debe permanecer en [10, 20, 30, 50]")
    if int(c.get("budget_resamples", 0)) != 100:
        raise ValueError("budget_resamples debe permanecer en 100")
    if not math.isclose(float(c.get("max_interpolated_fraction", -1)), 0.10):
        raise ValueError("max_interpolated_fraction debe permanecer en 0.10")
    if not math.isclose(float(c.get("rmse_threshold_m", -1)), 0.20):
        raise ValueError("rmse_threshold_m debe permanecer en 0.20 m")
    if not math.isclose(float(c.get("stability_tolerance_m", -1)), 0.02):
        raise ValueError("stability_tolerance_m debe permanecer en 0.02 m")
    if c.get("method") != "pchip":
        raise ValueError("method debe permanecer en pchip")
    if c.get("reserved_evaluated") is not False:
        raise ValueError("reserved_evaluated debe permanecer en false")
    return c


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--carpeta", type=Path, default=DEFAULT_QA)
    p.add_argument("--experimento", type=Path,
                   help="Si se omite, usa 07_continuidad_exploratoria/ultimo_experimento.txt")
    p.add_argument("--auditoria", type=Path,
                   help="Carpeta auditoria_* de etapa 14. Si se omite usa ultima_auditoria.txt")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return p


def locate(args):
    qa = args.carpeta.expanduser().resolve()
    ex = args.experimento.expanduser().resolve() if args.experimento else Path(
        (qa/"07_continuidad_exploratoria"/"ultimo_experimento.txt").read_text().strip()).resolve()
    audit = args.auditoria.expanduser().resolve() if args.auditoria else Path(
        (ex/"05_validacion_segmentos_v2"/"ultima_auditoria.txt").read_text().strip()).resolve()
    if not audit.is_relative_to(ex):
        raise ValueError("La auditoria no pertenece al experimento")
    summary = readjson(audit/"resumen.json")
    if not summary.get("etapa_completa") or summary.get("reserved_evaluated") is not False:
        raise ValueError("Auditoria incompleta o reserva alterada")
    for name, digest in summary.get("output_hashes", {}).items():
        p = audit/name
        if not p.exists() or sha(p) != digest:
            raise ValueError(f"Archivo de auditoria modificado: {name}")
    return qa, ex, audit, summary


def development_panel_complete(manifest_rows: list[dict], required_lengths: list[int]) -> bool:
    by_length = defaultdict(list)
    for row in manifest_rows:
        by_length[int(row["gap_length_days"])].append(row)
    if set(by_length) != set(required_lengths):
        return False
    for length in required_lengths:
        ranks = sorted(int(row["selection_rank"]) for row in by_length[length])
        if ranks != list(range(1, 51)):
            return False
    return True


def read_segment(audit: Path, s: dict) -> dict[date, dict]:
    p = (audit/s["segment_file_v2"]).resolve()
    if not p.is_relative_to(audit) or sha(p) != s["segment_sha256_v2"]:
        raise ValueError(f"Segmento cambiado: {s['segment_id']}")
    out = {}
    for r in readcsv(p):
        d = date.fromisoformat(r["date"])
        if d in out:
            raise ValueError(f"Fecha duplicada en {s['segment_id']}: {d}")
        out[d] = r
    return out


def evaluate_mask(mid: str, sid: str, L: int, m: dict, by_date: dict[date, dict], guard: int):
    start, end = date.fromisoformat(m["start_date"]), date.fromisoformat(m["end_date"])
    hidden_dates = [start + timedelta(days=i) for i in range(L)]
    left_dates = [start - timedelta(days=i) for i in range(guard, 0, -1)]
    right_dates = [end + timedelta(days=i) for i in range(1, guard+1)]
    all_dates = left_dates + hidden_dates + right_dates
    if any(d not in by_date for d in all_dates):
        raise ValueError(f"Mascara {m['mask_id']}: contexto ya no disponible")
    if any(phase(mid, d.year) != "desarrollo" for d in all_dates):
        raise ValueError(f"Mascara {m['mask_id']}: intento de tocar reserva")
    if any(not boolean(by_date[d]["eligible_truth"]) for d in all_dates):
        raise ValueError(f"Mascara {m['mask_id']}: truth/contexto no elegible")

    origin = start.toordinal()
    train_dates = left_dates + right_dates
    x_train = np.asarray([d.toordinal()-origin for d in train_dates], dtype=float)
    y_train = np.asarray([float(by_date[d]["depth_to_water_m"]) for d in train_dates], dtype=float)
    x_test = np.asarray([d.toordinal()-origin for d in hidden_dates], dtype=float)
    truth = np.asarray([float(by_date[d]["depth_to_water_m"]) for d in hidden_dates], dtype=float)
    pred = np.asarray(PchipInterpolator(x_train, y_train, extrapolate=False)(x_test), dtype=float)
    if not np.isfinite(pred).all() or not np.isfinite(truth).all():
        raise ValueError(f"Mascara {m['mask_id']}: prediccion/verdad no finita")
    e = pred-truth
    summary = dict(
        n_test=len(e), sse_m2=float(np.sum(e*e)), sae_m=float(np.sum(np.abs(e))),
        sum_error_m=float(np.sum(e)), rmse_m=float(np.sqrt(np.mean(e*e))),
        mae_m=float(np.mean(np.abs(e))), bias_m=float(np.mean(e)),
        p95_abs_error_m=float(np.quantile(np.abs(e), .95)),
    )
    days = [dict(
        monitoring_location_id=mid, segment_id=sid, gap_length_days=L,
        mask_id=m["mask_id"], date=d.isoformat(), observed_m=float(t),
        predicted_m=float(p), error_m=float(er), abs_error_m=float(abs(er)),
    ) for d, t, p, er in zip(hidden_dates, truth, pred, e)]
    return summary, days


def aggregate_masks(mask_rows: list[dict], day_abs_errors: list[float]):
    n = sum(int(r["n_test"]) for r in mask_rows)
    if not n:
        return dict(n_masks=0, n_days_evaluated=0, rmse_m="", mae_m="", bias_m="", p95_abs_error_m="")
    return dict(
        n_masks=len(mask_rows), n_days_evaluated=n,
        rmse_m=math.sqrt(sum(float(r["sse_m2"]) for r in mask_rows)/n),
        mae_m=sum(float(r["sae_m"]) for r in mask_rows)/n,
        bias_m=sum(float(r["sum_error_m"]) for r in mask_rows)/n,
        p95_abs_error_m=float(np.quantile(np.asarray(day_abs_errors, dtype=float), .95)),
    )


def subset_metrics(rows: list[dict], idx: np.ndarray):
    rs = [rows[int(i)] for i in idx]
    n = sum(int(r["n_test"]) for r in rs)
    return (
        math.sqrt(sum(float(r["sse_m2"]) for r in rs)/n),
        sum(float(r["sae_m"]) for r in rs)/n,
    )


def sensitivity(rows: list[dict], full_rmse: float, full_mae: float, threshold: float,
                tol: float, seed: int, mid: str, sid: str, L: int, n_resamples: int):
    out = []
    N = len(rows)
    full_pass = full_rmse <= threshold
    for B in BUDGETS:
        base = dict(monitoring_location_id=mid, segment_id=sid, gap_length_days=L,
                    budget_masks=B, n_full_masks=N, full_rmse_m=full_rmse, full_mae_m=full_mae)
        if N < B:
            out.append(dict(**base, n_resamples=0, q95_abs_delta_rmse_m="", q95_abs_delta_mae_m="",
                            mean_rmse_budget_m="", mean_mae_budget_m="", decision_flip_fraction_vs_full="",
                            stability_le_0_02_rmse="", stability_le_0_02_mae="", budget_evaluable=0,
                            has_larger_reference_panel=int(N>B)))
            continue
        if N == B:
            rm, ma = full_rmse, full_mae
            out.append(dict(**base, n_resamples=1, q95_abs_delta_rmse_m=0.0, q95_abs_delta_mae_m=0.0,
                            mean_rmse_budget_m=rm, mean_mae_budget_m=ma, decision_flip_fraction_vs_full=0.0,
                            stability_le_0_02_rmse=1, stability_le_0_02_mae=1, budget_evaluable=1,
                            has_larger_reference_panel=0))
            continue
        rng = stable_rng(seed, mid, sid, L, B, "budget")
        rmses, maes, flips = [], [], []
        for _ in range(n_resamples):
            idx = rng.choice(N, size=B, replace=False)
            rm, ma = subset_metrics(rows, idx)
            rmses.append(rm); maes.append(ma); flips.append((rm <= threshold) != full_pass)
        dr = np.abs(np.asarray(rmses)-full_rmse); da = np.abs(np.asarray(maes)-full_mae)
        qdr, qda = float(np.quantile(dr,.95)), float(np.quantile(da,.95))
        out.append(dict(**base, n_resamples=n_resamples,
                        q95_abs_delta_rmse_m=qdr, q95_abs_delta_mae_m=qda,
                        mean_rmse_budget_m=float(np.mean(rmses)), mean_mae_budget_m=float(np.mean(maes)),
                        decision_flip_fraction_vs_full=float(np.mean(flips)),
                        stability_le_0_02_rmse=int(qdr<=tol), stability_le_0_02_mae=int(qda<=tol),
                        budget_evaluable=1, has_larger_reference_panel=1))
    return out


def write_day_gz(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+".tmp")
    with tmp.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            import io
            with io.TextIOWrapper(gz, encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=DAY_FIELDS); w.writeheader(); w.writerows(rows)
    tmp.replace(path)


def main():
    args = parser().parse_args()
    c = configuration(args.config.resolve())
    qa, ex, audit, audit_summary = locate(args)
    if audit_summary.get("config") != c:
        raise ValueError("La configuracion de desarrollo no coincide con la usada para congelar las mascaras")
    segs = list(readcsv(audit/"segmentos_elegibles_30d.csv"))
    masks = list(readcsv(audit/"manifiesto_ventanas_desarrollo.csv"))
    masks_by_sid = defaultdict(list)
    for m in masks:
        if m["phase"] != "desarrollo":
            raise ValueError("El manifiesto contiene una mascara fuera de desarrollo")
        masks_by_sid[m["segment_id"]].append(m)

    spec = dict(
        version=VERSION, config=c,
        inputs={
            "audit_summary": sha(audit/"resumen.json"),
            "segments": sha(audit/"segmentos_elegibles_30d.csv"),
            "masks": sha(audit/"manifiesto_ventanas_desarrollo.csv"),
        },
        script_sha256=sha(Path(__file__)), python=platform.python_version(),
        numpy=np.__version__, scipy=scipy.__version__,
    )
    token = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    root = ex/"06_desarrollo_todos_segmentos_v2"/("desarrollo_"+token)
    root.mkdir(parents=True, exist_ok=True)
    writejson(root/"resumen.json", dict(etapa_completa=False, inicio_utc=now(), reserved_evaluated=False, **spec))

    reports = {}; errors = []
    for i, s in enumerate(segs, 1):
        sid, mid = s["segment_id"], s["monitoring_location_id"]
        folder = root/"por_segmento"/sid
        checkpoint = folder/"checkpoint.json"
        try:
            manifest_rows = sorted(masks_by_sid.get(sid, []), key=lambda r:(int(r["gap_length_days"]), int(r["selection_rank"])))
            manifest_digest = hashlib.sha256(json.dumps(manifest_rows, sort_keys=True).encode()).hexdigest()
            saved = readjson(checkpoint) if checkpoint.exists() else {}
            reusable = (saved.get("etapa_completa") and saved.get("source_sha256") == s["segment_sha256_v2"]
                        and saved.get("mask_manifest_digest") == manifest_digest
                        and saved.get("script_sha256") == spec["script_sha256"]
                        and all((folder/name).exists() and sha(folder/name)==h for name,h in saved.get("output_hashes",{}).items()))
            if reusable:
                reports[sid] = saved
                continue

            folder.mkdir(parents=True, exist_ok=True)
            # Segmentos sin huecos no necesitan validacion de interpolacion.
            if int(s["missing_days"]) == 0:
                writecsv(folder/"metricas_mascaras.csv", [], MASK_METRIC_FIELDS)
                write_day_gz(folder/"predicciones_dia.csv.gz", [])
                report = dict(etapa_completa=True, monitoring_location_id=mid, segment_id=sid,
                              status="sin_interpolacion_necesaria", source_sha256=s["segment_sha256_v2"],
                              mask_manifest_digest=manifest_digest, script_sha256=spec["script_sha256"],
                              reserved_evaluated=False, output_hashes={})
                report["output_hashes"] = {n:sha(folder/n) for n in ["metricas_mascaras.csv","predicciones_dia.csv.gz"]}
                writejson(checkpoint, report); reports[sid]=report; continue

            if float(s["fraction_to_interpolate"]) > float(c["max_interpolated_fraction"]):
                writecsv(folder/"metricas_mascaras.csv", [], MASK_METRIC_FIELDS)
                write_day_gz(folder/"predicciones_dia.csv.gz", [])
                report = dict(etapa_completa=True, monitoring_location_id=mid, segment_id=sid,
                              status="excede_fraccion_interpolada", source_sha256=s["segment_sha256_v2"],
                              mask_manifest_digest=manifest_digest, script_sha256=spec["script_sha256"],
                              reserved_evaluated=False, output_hashes={})
                report["output_hashes"] = {n:sha(folder/n) for n in ["metricas_mascaras.csv","predicciones_dia.csv.gz"]}
                writejson(checkpoint, report); reports[sid]=report; continue

            required = relevant_lengths(int(s["max_gap_missing_days"]))
            if not development_panel_complete(manifest_rows, required):
                writecsv(folder/"metricas_mascaras.csv", [], MASK_METRIC_FIELDS)
                write_day_gz(folder/"predicciones_dia.csv.gz", [])
                report = dict(etapa_completa=True, monitoring_location_id=mid, segment_id=sid,
                              status="ventanas_desarrollo_insuficientes", source_sha256=s["segment_sha256_v2"],
                              mask_manifest_digest=manifest_digest, script_sha256=spec["script_sha256"],
                              n_masks=len(manifest_rows), n_days_evaluated=0, reserved_evaluated=False,
                              output_hashes={})
                report["output_hashes"] = {n:sha(folder/n) for n in ["metricas_mascaras.csv","predicciones_dia.csv.gz"]}
                writejson(checkpoint, report); reports[sid]=report; continue

            by_date = read_segment(audit, s)
            mask_metrics, day_rows = [], []
            for m in manifest_rows:
                L = int(m["gap_length_days"])
                sm, days = evaluate_mask(mid, sid, L, m, by_date, int(c["guard_days"]))
                mask_metrics.append(dict(
                    monitoring_location_id=mid, site_no=s["site_no"], estado=s["estado"], segment_id=sid,
                    gap_length_days=L, mask_id=m["mask_id"], start_date=m["start_date"], end_date=m["end_date"],
                    year=m["year"], quarter=m["quarter"], segment_zone=m["segment_zone"], **sm,
                ))
                day_rows.extend(days)
            writecsv(folder/"metricas_mascaras.csv", mask_metrics, MASK_METRIC_FIELDS)
            write_day_gz(folder/"predicciones_dia.csv.gz", day_rows)
            report = dict(etapa_completa=True, monitoring_location_id=mid, segment_id=sid,
                          status="evaluado_desarrollo", source_sha256=s["segment_sha256_v2"],
                          mask_manifest_digest=manifest_digest, script_sha256=spec["script_sha256"],
                          n_masks=len(mask_metrics), n_days_evaluated=len(day_rows), reserved_evaluated=False,
                          output_hashes={n:sha(folder/n) for n in ["metricas_mascaras.csv","predicciones_dia.csv.gz"]})
            writejson(checkpoint, report); reports[sid]=report
        except (ValueError, OSError, KeyError, FloatingPointError) as exc:
            errors.append(dict(monitoring_location_id=mid, segment_id=sid, error=str(exc)))
        if i % 10 == 0 or i == len(segs):
            print(f"Validacion desarrollo: {i}/{len(segs)} segmentos; errores {len(errors)}", flush=True)

    if errors:
        writecsv(root/"errores.csv", errors, ["monitoring_location_id","segment_id","error"])
        writejson(root/"resumen.json", dict(etapa_completa=False, errores=len(errors), reserved_evaluated=False, **spec))
        raise ValueError(f"Errores tecnicos; revisar {root/'errores.csv'}")

    # Agregacion final sin cargar predicciones diarias nacionales.
    segment_metrics, sens_rows = [], []
    seg_by_sid = {s["segment_id"]:s for s in segs}
    status_rows = []
    for sid, s in seg_by_sid.items():
        report = reports[sid]
        status_rows.append(dict(monitoring_location_id=s["monitoring_location_id"], segment_id=sid,
                                status=report["status"], fraction_to_interpolate=s["fraction_to_interpolate"],
                                missing_days=s["missing_days"], max_gap_missing_days=s["max_gap_missing_days"]))
        if report["status"] != "evaluado_desarrollo":
            continue
        folder = root/"por_segmento"/sid
        mrows = list(readcsv(folder/"metricas_mascaras.csv"))
        # p95 exacto se obtiene de errores diarios del propio segmento, agrupados por longitud.
        day_abs = defaultdict(list)
        mask_to_L = {r["mask_id"]:int(r["gap_length_days"]) for r in mrows}
        for d in readcsv(folder/"predicciones_dia.csv.gz"):
            day_abs[mask_to_L[d["mask_id"]]].append(float(d["abs_error_m"]))
        groups = defaultdict(list)
        for r in mrows: groups[int(r["gap_length_days"])].append(r)
        for L, rs in sorted(groups.items()):
            agg = aggregate_masks(rs, day_abs[L])
            segment_metrics.append(dict(
                monitoring_location_id=s["monitoring_location_id"], site_no=s["site_no"], estado=s["estado"],
                segment_id=sid, gap_length_days=L, **agg,
                rmse_le_0_20=int(float(agg["rmse_m"]) <= float(c["rmse_threshold_m"])),
                fraction_to_interpolate=s["fraction_to_interpolate"], max_gap_missing_days=s["max_gap_missing_days"],
            ))
            sens_rows.extend(sensitivity(
                rs, float(agg["rmse_m"]), float(agg["mae_m"]), float(c["rmse_threshold_m"]),
                float(c["stability_tolerance_m"]), int(c["seed"]), s["monitoring_location_id"], sid, L,
                int(c["budget_resamples"]),
            ))

    writecsv(root/"estado_segmentos.csv", status_rows,
             ["monitoring_location_id","segment_id","status","fraction_to_interpolate","missing_days","max_gap_missing_days"])
    writecsv(root/"metricas_segmento_longitud.csv", segment_metrics, SEGMENT_METRIC_FIELDS)
    writecsv(root/"sensibilidad_presupuesto.csv", sens_rows, SENS_FIELDS)
    writecsv(root/"errores.csv", [], ["monitoring_location_id","segment_id","error"])

    # Resumen nacional para escoger UN presupuesto comun, no para seleccionarlo automaticamente.
    national = []
    for B in BUDGETS:
        rs = [r for r in sens_rows if int(r["budget_masks"])==B and int(r["budget_evaluable"])==1]
        larger = [r for r in rs if int(r["has_larger_reference_panel"])==1]
        national.append(dict(
            budget_masks=B,
            n_segment_lengths_evaluable=len(rs),
            n_with_larger_reference_panel=len(larger),
            pct_stable_rmse=100*sum(int(r["stability_le_0_02_rmse"]) for r in larger)/len(larger) if larger else "",
            pct_stable_mae=100*sum(int(r["stability_le_0_02_mae"]) for r in larger)/len(larger) if larger else "",
            mean_decision_flip_pct=100*np.mean([float(r["decision_flip_fraction_vs_full"]) for r in larger]) if larger else "",
            p95_decision_flip_pct=100*np.quantile([float(r["decision_flip_fraction_vs_full"]) for r in larger],.95) if larger else "",
            median_q95_abs_delta_rmse_m=float(np.median([float(r["q95_abs_delta_rmse_m"]) for r in larger])) if larger else "",
            p95_q95_abs_delta_rmse_m=float(np.quantile([float(r["q95_abs_delta_rmse_m"]) for r in larger],.95)) if larger else "",
        ))
    writecsv(root/"resumen_sensibilidad_presupuesto.csv", national,
             ["budget_masks","n_segment_lengths_evaluable","n_with_larger_reference_panel","pct_stable_rmse",
              "pct_stable_mae","mean_decision_flip_pct","p95_decision_flip_pct",
              "median_q95_abs_delta_rmse_m","p95_q95_abs_delta_rmse_m"])

    outputs = [root/"estado_segmentos.csv", root/"metricas_segmento_longitud.csv",
               root/"sensibilidad_presupuesto.csv", root/"resumen_sensibilidad_presupuesto.csv", root/"errores.csv"]
    summary = dict(
        etapa_completa=True, finalizado_utc=now(), reserved_evaluated=False, final_validation=False,
        **spec,
        n_segments=len(segs),
        statuses=dict(Counter(r["status"] for r in status_rows)),
        n_segment_length_metrics=len(segment_metrics),
        n_mask_evaluations=sum(r.get("n_masks",0) for r in reports.values()),
        output_hashes={p.name:sha(p) for p in outputs},
        checkpoints_sha256={sid:sha(root/"por_segmento"/sid/"checkpoint.json") for sid in reports},
        note="No se eligio presupuesto final. No se abrio reserva. No se rellenaron huecos reales.",
    )
    writejson(root/"resumen.json", summary)
    pointer = ex/"06_desarrollo_todos_segmentos_v2"/"ultimo_desarrollo.txt"
    tmp = pointer.with_suffix(".tmp"); tmp.write_text(str(root)+"\n", encoding="utf-8"); tmp.replace(pointer)

    print("\nDESARROLLO COMPLETO; RESERVA NO ABIERTA")
    print(json.dumps(summary["statuses"], indent=2, ensure_ascii=False))
    print("Resultados:", root)
    print("Traer: resumen_sensibilidad_presupuesto.csv, sensibilidad_presupuesto.csv, metricas_segmento_longitud.csv y estado_segmentos.csv")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrumpido. Los checkpoints por segmento permiten reanudar.", file=sys.stderr); sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr); sys.exit(2)
