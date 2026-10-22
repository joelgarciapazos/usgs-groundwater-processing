#!/usr/bin/env python3
"""
Comparación pareada y de solo lectura entre interpolación lineal, PCHIP y Akima
sobre las máscaras de DESARROLLO congeladas del workflow final.

Objetivo
--------
Actualizar el antiguo piloto 09_piloto_interpolacion.py para que sea plenamente
compatible con el proceso final de validación (scripts 07-08 de v1.0.0):

- reutiliza EXACTAMENTE las máscaras de desarrollo ya congeladas;
- usa el mismo contexto 3+3 de observaciones elegibles;
- nunca abre ni evalúa los años reservados;
- no modifica selección de segmentos, aceptación, membresía del dataset ni el
  método PCHIP de producción;
- compara lineal, PCHIP y Akima de forma pareada sobre las mismas ventanas;
- verifica que el PCHIP recalculado reproduce las métricas de desarrollo ya
  congeladas antes de aceptar la comparación;
- genera resúmenes descriptivos adecuados para documentar la elección del
  método en el manuscrito, sin afirmar superioridad estadística.

Uso recomendado
---------------
Esta es la adaptación pública del generador histórico de Table A2. Ejecutar:

python3 analysis/compare_interpolation_methods_frozen_masks.py \
  --carpeta /ruta/a/la/carpeta/QA

Opcionalmente se pueden proporcionar --experimento, --auditoria y --desarrollo.
La lógica científica se conserva; solo se adapta la importación del módulo común
al nombre público utilizado por este repositorio.

IMPORTANTE
----------
Este análisis es post hoc y descriptivo. No cambia el workflow congelado de
Version 1.0 y no debe usarse para reabrir la reserva ni alterar la membresía.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import platform
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import numpy as np
import scipy
from scipy.interpolate import Akima1DInterpolator, PchipInterpolator

from continuity_common import DEFAULT_QA, boolean, now, readcsv, readjson, sha, writecsv, writejson

VERSION = "3.0.0"
METHODS = ("linear", "pchip", "akima")
ANCHOR_LENGTHS = (1, 3, 7, 15, 30)
PCHIP_TOL = 1e-12

MASK_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id",
    "gap_length_days", "mask_id", "start_date", "end_date", "year",
    "quarter", "segment_zone", "method", "n_test", "sse_m2", "sae_m",
    "sum_error_m", "rmse_m", "mae_m", "bias_m", "p95_abs_error_m",
]

SEGMENT_LENGTH_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id",
    "gap_length_days", "method", "n_masks", "n_days_evaluated",
    "rmse_m", "mae_m", "bias_m", "p95_abs_error_m",
]

DAY_FIELDS = [
    "monitoring_location_id", "segment_id", "gap_length_days", "mask_id",
    "method", "date", "observed_m", "predicted_m", "error_m",
    "abs_error_m",
]


def phase(mid: str, year: int) -> str:
    """Regla determinista congelada: 1/5 de los años reservado por pozo."""
    offset = int(hashlib.sha256(mid.encode()).hexdigest()[:8], 16) % 5
    return "reservada" if year % 5 == offset else "desarrollo"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--carpeta", type=Path, default=DEFAULT_QA,
                   help="Raíz QA congelada usada por el workflow final")
    p.add_argument("--experimento", type=Path,
                   help="Experimento de continuidad; si se omite usa ultimo_experimento.txt")
    p.add_argument("--auditoria", type=Path,
                   help="Carpeta auditoria_* de 05_validacion_segmentos_v2")
    p.add_argument("--desarrollo", type=Path,
                   help="Carpeta desarrollo_* de 06_desarrollo_todos_segmentos_v2")
    p.add_argument("--salida", type=Path,
                   help="Carpeta de salida; por defecto se crea dentro del experimento")
    p.add_argument("--guardar-predicciones-dia", action="store_true",
                   help="Guarda predicciones diarias de los tres métodos (archivo grande)")
    return p


def locate(args):
    qa = args.carpeta.expanduser().resolve()

    ex = args.experimento.expanduser().resolve() if args.experimento else Path(
        (qa / "07_continuidad_exploratoria" / "ultimo_experimento.txt")
        .read_text(encoding="utf-8-sig").strip()
    ).resolve()

    audit = args.auditoria.expanduser().resolve() if args.auditoria else Path(
        (ex / "05_validacion_segmentos_v2" / "ultima_auditoria.txt")
        .read_text(encoding="utf-8-sig").strip()
    ).resolve()

    dev = args.desarrollo.expanduser().resolve() if args.desarrollo else Path(
        (ex / "06_desarrollo_todos_segmentos_v2" / "ultimo_desarrollo.txt")
        .read_text(encoding="utf-8-sig").strip()
    ).resolve()

    if not audit.is_relative_to(ex):
        raise ValueError("La auditoría no pertenece al experimento seleccionado")
    if not dev.is_relative_to(ex):
        raise ValueError("La validación de desarrollo no pertenece al experimento seleccionado")

    audit_summary = readjson(audit / "resumen.json")
    dev_summary = readjson(dev / "resumen.json")

    if not audit_summary.get("etapa_completa"):
        raise ValueError("La auditoría de máscaras no está completa")
    if audit_summary.get("reserved_evaluated") is not False:
        raise ValueError("La auditoría indica que la reserva fue evaluada")
    if not dev_summary.get("etapa_completa"):
        raise ValueError("La validación de desarrollo no está completa")
    if dev_summary.get("reserved_evaluated") is not False:
        raise ValueError("La etapa de desarrollo indica que la reserva fue evaluada")

    for name, digest in audit_summary.get("output_hashes", {}).items():
        p = audit / name
        if not p.exists() or sha(p) != digest:
            raise ValueError(f"Archivo de auditoría modificado: {name}")

    for name, digest in dev_summary.get("output_hashes", {}).items():
        p = dev / name
        if not p.exists() or sha(p) != digest:
            raise ValueError(f"Archivo de desarrollo modificado: {name}")

    # Verificar que el desarrollo realmente se construyó desde esta auditoría.
    expected_audit_hash = dev_summary.get("inputs", {}).get("audit_summary")
    if expected_audit_hash and expected_audit_hash != sha(audit / "resumen.json"):
        raise ValueError("La etapa de desarrollo no corresponde a la auditoría seleccionada")

    return qa, ex, audit, dev, audit_summary, dev_summary


def read_segment(audit: Path, s: dict) -> dict[date, dict]:
    p = (audit / s["segment_file_v2"]).resolve()
    if not p.is_relative_to(audit):
        raise ValueError(f"Ruta de segmento fuera de auditoría: {s['segment_id']}")
    if sha(p) != s["segment_sha256_v2"]:
        raise ValueError(f"Segmento modificado: {s['segment_id']}")

    out = {}
    for r in readcsv(p):
        d = date.fromisoformat(r["date"])
        if d in out:
            raise ValueError(f"Fecha duplicada en {s['segment_id']}: {d}")
        out[d] = r
    return out


def predictions_for_mask(mid: str, m: dict, by_date: dict[date, dict], guard: int = 3):
    """Predice la misma máscara con los tres métodos usando el mismo contexto 3+3."""
    L = int(m["gap_length_days"])
    start = date.fromisoformat(m["start_date"])
    end = date.fromisoformat(m["end_date"])

    hidden = [start + timedelta(days=i) for i in range(L)]
    left = [start - timedelta(days=i) for i in range(guard, 0, -1)]
    right = [end + timedelta(days=i) for i in range(1, guard + 1)]
    all_dates = left + hidden + right

    if any(d not in by_date for d in all_dates):
        raise ValueError(f"Máscara {m['mask_id']}: contexto ya no disponible")
    if any(phase(mid, d.year) != "desarrollo" for d in all_dates):
        raise ValueError(f"Máscara {m['mask_id']}: intento de tocar reserva")
    if any(not boolean(by_date[d]["eligible_truth"]) for d in all_dates):
        raise ValueError(f"Máscara {m['mask_id']}: target/contexto no elegible")

    origin = start.toordinal()
    train_dates = left + right
    x_train = np.asarray([d.toordinal() - origin for d in train_dates], dtype=float)
    y_train = np.asarray([float(by_date[d]["depth_to_water_m"]) for d in train_dates], dtype=float)
    x_test = np.asarray([d.toordinal() - origin for d in hidden], dtype=float)
    truth = np.asarray([float(by_date[d]["depth_to_water_m"]) for d in hidden], dtype=float)

    preds = {
        "linear": np.interp(x_test, x_train, y_train),
        "pchip": np.asarray(PchipInterpolator(x_train, y_train, extrapolate=False)(x_test), dtype=float),
        "akima": np.asarray(Akima1DInterpolator(x_train, y_train, extrapolate=False)(x_test), dtype=float),
    }

    if not np.isfinite(truth).all():
        raise ValueError(f"Máscara {m['mask_id']}: truth no finita")
    for method, pred in preds.items():
        if pred.shape != truth.shape or not np.isfinite(pred).all():
            raise ValueError(f"Máscara {m['mask_id']}: predicción inválida para {method}")

    return truth, preds, hidden


def score(truth: np.ndarray, pred: np.ndarray) -> dict:
    err = pred - truth
    ae = np.abs(err)
    return dict(
        n_test=len(err),
        sse_m2=float(np.sum(err ** 2)),
        sae_m=float(np.sum(ae)),
        sum_error_m=float(np.sum(err)),
        rmse_m=float(np.sqrt(np.mean(err ** 2))),
        mae_m=float(np.mean(ae)),
        bias_m=float(np.mean(err)),
        p95_abs_error_m=float(np.quantile(ae, 0.95)),
    )


def aggregate(rows: list[dict], abs_errors: list[float] | None = None) -> dict:
    n = sum(int(r["n_test"]) for r in rows)
    if n <= 0:
        raise ValueError("No hay días para agregar")
    out = dict(
        n_masks=len(rows),
        n_days_evaluated=n,
        rmse_m=math.sqrt(sum(float(r["sse_m2"]) for r in rows) / n),
        mae_m=sum(float(r["sae_m"]) for r in rows) / n,
        bias_m=sum(float(r["sum_error_m"]) for r in rows) / n,
    )
    out["p95_abs_error_m"] = (
        float(np.quantile(np.asarray(abs_errors, dtype=float), 0.95))
        if abs_errors else ""
    )
    return out


def write_day_gz(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with io.TextIOWrapper(gz, encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=DAY_FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
    tmp.replace(path)


def pchip_reconciliation(seg_metrics: list[dict], frozen_path: Path):
    """Comprueba que nuestro PCHIP reproduce el desarrollo congelado."""
    frozen = list(readcsv(frozen_path))
    frozen_index = {
        (r["segment_id"], int(r["gap_length_days"])): r
        for r in frozen
    }
    ours = {
        (r["segment_id"], int(r["gap_length_days"])): r
        for r in seg_metrics if r["method"] == "pchip"
    }

    if set(ours) != set(frozen_index):
        only_ours = sorted(set(ours) - set(frozen_index))[:10]
        only_frozen = sorted(set(frozen_index) - set(ours))[:10]
        raise ValueError(
            "Las unidades segmento-longitud PCHIP no coinciden con el desarrollo congelado. "
            f"Solo nuevas={only_ours}; solo congeladas={only_frozen}"
        )

    max_d_rmse = 0.0
    max_d_mae = 0.0
    max_d_bias = 0.0
    for key in ours:
        a, b = ours[key], frozen_index[key]
        dr = abs(float(a["rmse_m"]) - float(b["rmse_m"]))
        dm = abs(float(a["mae_m"]) - float(b["mae_m"]))
        db = abs(float(a["bias_m"]) - float(b["bias_m"]))
        max_d_rmse = max(max_d_rmse, dr)
        max_d_mae = max(max_d_mae, dm)
        max_d_bias = max(max_d_bias, db)
        if dr > PCHIP_TOL or dm > PCHIP_TOL or db > PCHIP_TOL:
            raise ValueError(
                f"PCHIP no reproduce desarrollo congelado en {key}: "
                f"ΔRMSE={dr}, ΔMAE={dm}, Δbias={db}"
            )

    return dict(
        n_segment_length_units=len(ours),
        tolerance=PCHIP_TOL,
        max_abs_delta_rmse_m=max_d_rmse,
        max_abs_delta_mae_m=max_d_mae,
        max_abs_delta_bias_m=max_d_bias,
        reconciled=True,
    )


def main():
    args = parser().parse_args()
    qa, ex, audit, dev, audit_summary, dev_summary = locate(args)

    segs = list(readcsv(audit / "segmentos_elegibles_30d.csv"))
    seg_by_sid = {r["segment_id"]: r for r in segs}

    status_rows = list(readcsv(dev / "estado_segmentos.csv"))
    evaluated_ids = {
        r["segment_id"] for r in status_rows if r["status"] == "evaluado_desarrollo"
    }
    if not evaluated_ids:
        raise ValueError("No hay segmentos con status=evaluado_desarrollo")

    masks = list(readcsv(audit / "manifiesto_ventanas_desarrollo.csv"))
    masks_by_sid = defaultdict(list)
    for m in masks:
        if m["phase"] != "desarrollo":
            raise ValueError("El manifiesto contiene una máscara fuera de desarrollo")
        if m["segment_id"] in evaluated_ids:
            masks_by_sid[m["segment_id"]].append(m)

    missing_segments = sorted(evaluated_ids - set(masks_by_sid))
    if missing_segments:
        raise ValueError(f"Segmentos evaluados sin máscaras: {missing_segments[:10]}")

    spec = dict(
        version=VERSION,
        analysis_type="READ_ONLY_PAIRED_INTERPOLATION_METHOD_COMPARISON",
        methods=list(METHODS),
        development_only=True,
        reserved_evaluated=False,
        changes_dataset_membership=False,
        changes_production_method=False,
        audit_summary_sha256=sha(audit / "resumen.json"),
        development_summary_sha256=sha(dev / "resumen.json"),
        development_masks_sha256=sha(audit / "manifiesto_ventanas_desarrollo.csv"),
        frozen_pchip_metrics_sha256=sha(dev / "metricas_segmento_longitud.csv"),
        script_sha256=sha(Path(__file__)),
        python=platform.python_version(),
        numpy=np.__version__,
        scipy=scipy.__version__,
        interpretation=(
            "Descriptive paired comparison on the exact frozen development masks. "
            "No reserved data are opened and Version 1.0 membership is unchanged."
        ),
    )
    token = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    out = args.salida.expanduser().resolve() if args.salida else (
        ex / "07_comparacion_metodos_interpolacion" / f"metodos_{token}"
    )
    out.mkdir(parents=True, exist_ok=True)
    writejson(out / "resumen.json", dict(etapa_completa=False, inicio_utc=now(), **spec))

    mask_metrics = []
    day_rows = []
    errors = []

    ordered_ids = sorted(evaluated_ids)
    for i, sid in enumerate(ordered_ids, 1):
        s = seg_by_sid.get(sid)
        if s is None:
            errors.append(dict(segment_id=sid, monitoring_location_id="", error="segmento_no_en_inventario"))
            continue
        mid = s["monitoring_location_id"]
        try:
            by_date = read_segment(audit, s)
            manifest = sorted(
                masks_by_sid[sid],
                key=lambda r: (int(r["gap_length_days"]), int(r["selection_rank"]))
            )
            for m in manifest:
                truth, preds, hidden = predictions_for_mask(mid, m, by_date, guard=3)
                for method in METHODS:
                    pred = preds[method]
                    sm = score(truth, pred)
                    mask_metrics.append(dict(
                        monitoring_location_id=mid,
                        site_no=s["site_no"],
                        estado=s["estado"],
                        segment_id=sid,
                        gap_length_days=int(m["gap_length_days"]),
                        mask_id=m["mask_id"],
                        start_date=m["start_date"],
                        end_date=m["end_date"],
                        year=m["year"],
                        quarter=m["quarter"],
                        segment_zone=m["segment_zone"],
                        method=method,
                        **sm,
                    ))
                    if args.guardar_predicciones_dia:
                        err = pred - truth
                        for d, obs, est, er in zip(hidden, truth, pred, err):
                            day_rows.append(dict(
                                monitoring_location_id=mid,
                                segment_id=sid,
                                gap_length_days=int(m["gap_length_days"]),
                                mask_id=m["mask_id"],
                                method=method,
                                date=d.isoformat(),
                                observed_m=float(obs),
                                predicted_m=float(est),
                                error_m=float(er),
                                abs_error_m=float(abs(er)),
                            ))
        except (ValueError, OSError, KeyError, FloatingPointError) as exc:
            errors.append(dict(segment_id=sid, monitoring_location_id=mid, error=str(exc)))

        if i % 20 == 0 or i == len(ordered_ids):
            print(f"Métodos {i}/{len(ordered_ids)} segmentos; errores {len(errors)}", flush=True)

    if errors:
        writecsv(out / "errores.csv", errors, ["segment_id", "monitoring_location_id", "error"])
        writejson(out / "resumen.json", dict(etapa_completa=False, errores=len(errors), **spec))
        raise ValueError(f"Hay errores; revisar {out/'errores.csv'}")

    # Integridad pareada: cada máscara debe tener exactamente los tres métodos.
    methods_by_mask = defaultdict(set)
    for r in mask_metrics:
        methods_by_mask[(r["segment_id"], int(r["gap_length_days"]), r["mask_id"])].add(r["method"])
    unpaired = [k for k, ms in methods_by_mask.items() if ms != set(METHODS)]
    if unpaired:
        raise ValueError(f"Comparación no pareada en {len(unpaired)} máscaras")

    writecsv(out / "metricas_mascara_metodo.csv", mask_metrics, MASK_FIELDS)
    if args.guardar_predicciones_dia:
        write_day_gz(out / "predicciones_dia_metodo.csv.gz", day_rows)

    # Agregación por segmento x longitud x método, idéntica en lógica al workflow final.
    grouped = defaultdict(list)
    for r in mask_metrics:
        grouped[(
            r["monitoring_location_id"], r["site_no"], r["estado"],
            r["segment_id"], int(r["gap_length_days"]), r["method"]
        )].append(r)

    seg_metrics = []
    for (mid, site_no, estado, sid, L, method), rs in sorted(grouped.items()):
        # Para p95 exacto a nivel pooled, reconstruimos errores absolutos desde las métricas de máscara
        # solo si se guardaron puntos; no es necesario para la comparación principal.
        agg = aggregate(rs)
        seg_metrics.append(dict(
            monitoring_location_id=mid,
            site_no=site_no,
            estado=estado,
            segment_id=sid,
            gap_length_days=L,
            method=method,
            **agg,
        ))
    writecsv(out / "metricas_segmento_longitud_metodo.csv", seg_metrics, SEGMENT_LENGTH_FIELDS)

    # Reconciliación obligatoria: nuestro PCHIP debe reproducir el desarrollo congelado.
    reconciliation = pchip_reconciliation(seg_metrics, dev / "metricas_segmento_longitud.csv")
    writejson(out / "reconciliacion_pchip.json", reconciliation)

    # Resumen por longitud (incluye longitudes no ancla cuando G las requiere).
    by_len_method = defaultdict(list)
    for r in seg_metrics:
        by_len_method[(int(r["gap_length_days"]), r["method"])].append(r)

    length_rows = []
    for (L, method), rs in sorted(by_len_method.items()):
        # Pool exacto desde todas las máscaras de esa longitud y método.
        mr = [x for x in mask_metrics if int(x["gap_length_days"]) == L and x["method"] == method]
        p = aggregate(mr)
        length_rows.append(dict(
            gap_length_days=L,
            anchor_length=int(L in ANCHOR_LENGTHS),
            method=method,
            n_segments=len(rs),
            n_masks=p["n_masks"],
            n_days_evaluated=p["n_days_evaluated"],
            pooled_rmse_m=p["rmse_m"],
            pooled_mae_m=p["mae_m"],
            pooled_bias_m=p["bias_m"],
            mean_segment_rmse_m=float(np.mean([float(x["rmse_m"]) for x in rs])),
            median_segment_rmse_m=float(np.median([float(x["rmse_m"]) for x in rs])),
            mean_segment_mae_m=float(np.mean([float(x["mae_m"]) for x in rs])),
            median_segment_mae_m=float(np.median([float(x["mae_m"]) for x in rs])),
        ))

    LENGTH_FIELDS = [
        "gap_length_days", "anchor_length", "method", "n_segments", "n_masks",
        "n_days_evaluated", "pooled_rmse_m", "pooled_mae_m", "pooled_bias_m",
        "mean_segment_rmse_m", "median_segment_rmse_m",
        "mean_segment_mae_m", "median_segment_mae_m",
    ]
    writecsv(out / "resumen_metodo_longitud.csv", length_rows, LENGTH_FIELDS)
    writecsv(
        out / "resumen_metodo_longitudes_ancla.csv",
        [r for r in length_rows if int(r["anchor_length"]) == 1],
        LENGTH_FIELDS,
    )

    # Resumen global por método: pooling de TODOS los días enmascarados de desarrollo.
    global_rows = []
    for method in METHODS:
        mr = [x for x in mask_metrics if x["method"] == method]
        sr = [x for x in seg_metrics if x["method"] == method]
        p = aggregate(mr)
        global_rows.append(dict(
            method=method,
            n_segments=len({x["segment_id"] for x in sr}),
            n_segment_length_units=len(sr),
            n_masks=p["n_masks"],
            n_days_evaluated=p["n_days_evaluated"],
            pooled_rmse_m=p["rmse_m"],
            pooled_mae_m=p["mae_m"],
            pooled_bias_m=p["bias_m"],
            mean_segment_length_rmse_m=float(np.mean([float(x["rmse_m"]) for x in sr])),
            median_segment_length_rmse_m=float(np.median([float(x["rmse_m"]) for x in sr])),
            mean_segment_length_mae_m=float(np.mean([float(x["mae_m"]) for x in sr])),
            median_segment_length_mae_m=float(np.median([float(x["mae_m"]) for x in sr])),
        ))

    GLOBAL_FIELDS = [
        "method", "n_segments", "n_segment_length_units", "n_masks",
        "n_days_evaluated", "pooled_rmse_m", "pooled_mae_m", "pooled_bias_m",
        "mean_segment_length_rmse_m", "median_segment_length_rmse_m",
        "mean_segment_length_mae_m", "median_segment_length_mae_m",
    ]
    writecsv(out / "resumen_metodos_global.csv", global_rows, GLOBAL_FIELDS)

    # Comparación pareada contra lineal en la unidad segmento x longitud.
    unit = defaultdict(dict)
    for r in seg_metrics:
        key = (r["monitoring_location_id"], r["segment_id"], int(r["gap_length_days"]))
        unit[key][r["method"]] = r

    paired = []
    for (mid, sid, L), methods in sorted(unit.items()):
        if set(methods) != set(METHODS):
            raise ValueError(f"Unidad segmento-longitud no pareada: {mid}, {sid}, {L}")
        linear = methods["linear"]
        for method in ("pchip", "akima"):
            cur = methods[method]
            dr = float(cur["rmse_m"]) - float(linear["rmse_m"])
            dm = float(cur["mae_m"]) - float(linear["mae_m"])
            paired.append(dict(
                monitoring_location_id=mid,
                segment_id=sid,
                gap_length_days=L,
                method=method,
                delta_rmse_vs_linear_m=dr,
                delta_mae_vs_linear_m=dm,
                rmse_lower_than_linear=int(dr < 0),
                mae_lower_than_linear=int(dm < 0),
            ))

    PAIRED_FIELDS = [
        "monitoring_location_id", "segment_id", "gap_length_days", "method",
        "delta_rmse_vs_linear_m", "delta_mae_vs_linear_m",
        "rmse_lower_than_linear", "mae_lower_than_linear",
    ]
    writecsv(out / "comparacion_pareada_vs_lineal.csv", paired, PAIRED_FIELDS)

    paired_summary = []
    for method in ("pchip", "akima"):
        rs = [r for r in paired if r["method"] == method]
        paired_summary.append(dict(
            method=method,
            n_segment_length_units=len(rs),
            pct_rmse_lower_than_linear=100.0 * float(np.mean([int(r["rmse_lower_than_linear"]) for r in rs])),
            pct_mae_lower_than_linear=100.0 * float(np.mean([int(r["mae_lower_than_linear"]) for r in rs])),
            mean_delta_rmse_vs_linear_m=float(np.mean([float(r["delta_rmse_vs_linear_m"]) for r in rs])),
            median_delta_rmse_vs_linear_m=float(np.median([float(r["delta_rmse_vs_linear_m"]) for r in rs])),
            mean_delta_mae_vs_linear_m=float(np.mean([float(r["delta_mae_vs_linear_m"]) for r in rs])),
            median_delta_mae_vs_linear_m=float(np.median([float(r["delta_mae_vs_linear_m"]) for r in rs])),
        ))

    PAIRED_SUM_FIELDS = [
        "method", "n_segment_length_units", "pct_rmse_lower_than_linear",
        "pct_mae_lower_than_linear", "mean_delta_rmse_vs_linear_m",
        "median_delta_rmse_vs_linear_m", "mean_delta_mae_vs_linear_m",
        "median_delta_mae_vs_linear_m",
    ]
    writecsv(out / "resumen_pareado_vs_lineal.csv", paired_summary, PAIRED_SUM_FIELDS)

    # Tabla compacta pensada para manuscript/reviewer response.
    pair_map = {r["method"]: r for r in paired_summary}
    manuscript_rows = []
    for r in global_rows:
        pm = pair_map.get(r["method"], {})
        manuscript_rows.append(dict(
            method=r["method"],
            n_segments=r["n_segments"],
            n_masks=r["n_masks"],
            n_masked_days=r["n_days_evaluated"],
            pooled_rmse_m=r["pooled_rmse_m"],
            pooled_mae_m=r["pooled_mae_m"],
            median_segment_length_rmse_m=r["median_segment_length_rmse_m"],
            median_segment_length_mae_m=r["median_segment_length_mae_m"],
            pct_rmse_lower_than_linear=pm.get("pct_rmse_lower_than_linear", ""),
            pct_mae_lower_than_linear=pm.get("pct_mae_lower_than_linear", ""),
        ))

    MANUSCRIPT_FIELDS = [
        "method", "n_segments", "n_masks", "n_masked_days",
        "pooled_rmse_m", "pooled_mae_m",
        "median_segment_length_rmse_m", "median_segment_length_mae_m",
        "pct_rmse_lower_than_linear", "pct_mae_lower_than_linear",
    ]
    writecsv(out / "tabla_comparacion_metodos_manuscrito.csv", manuscript_rows, MANUSCRIPT_FIELDS)

    outputs = [
        out / "metricas_mascara_metodo.csv",
        out / "metricas_segmento_longitud_metodo.csv",
        out / "resumen_metodo_longitud.csv",
        out / "resumen_metodo_longitudes_ancla.csv",
        out / "resumen_metodos_global.csv",
        out / "comparacion_pareada_vs_lineal.csv",
        out / "resumen_pareado_vs_lineal.csv",
        out / "tabla_comparacion_metodos_manuscrito.csv",
        out / "reconciliacion_pchip.json",
    ]
    if args.guardar_predicciones_dia:
        outputs.append(out / "predicciones_dia_metodo.csv.gz")

    final_summary = dict(
        etapa_completa=True,
        finalizado_utc=now(),
        **spec,
        n_segments_compared=len(evaluated_ids),
        n_unique_masks_compared=len(methods_by_mask),
        n_method_mask_evaluations=len(mask_metrics),
        paired_complete=True,
        pchip_reconciliation=reconciliation,
        output_hashes={p.name: sha(p) for p in outputs},
        note=(
            "Comparación descriptiva post hoc sobre desarrollo únicamente. "
            "No abre reserva, no rellena huecos reales y no altera Version 1.0."
        ),
    )
    writejson(out / "resumen.json", final_summary)

    print("\nCOMPARACIÓN DE MÉTODOS COMPLETA")
    print("Reserva abierta: NO")
    print("Membresía del dataset modificada: NO")
    print("Método de producción modificado: NO")
    print(f"Segmentos comparados: {len(evaluated_ids)}")
    print(f"Máscaras únicas comparadas: {len(methods_by_mask)}")
    print(f"PCHIP reconciliado a tolerancia {PCHIP_TOL}: SI")
    print("Resultados:", out)
    print("\nTraer para revisión:")
    print(" - tabla_comparacion_metodos_manuscrito.csv")
    print(" - resumen_metodos_global.csv")
    print(" - resumen_metodo_longitudes_ancla.csv")
    print(" - resumen_pareado_vs_lineal.csv")
    print(" - reconciliacion_pchip.json")
    print(" - resumen.json")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
