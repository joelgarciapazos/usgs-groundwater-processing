#!/usr/bin/env python3
"""Interpolate accepted natural internal gaps with PCHIP and export the final daily dataset.

PCHIP uses all retained source values within each accepted segment, no extrapolation is allowed, source values are preserved, gaps are limited to <=30 days, and wells that failed reserved validation remain excluded."""
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
from collections import Counter
from datetime import date
from pathlib import Path
from statistics import mean, median

import numpy as np
import scipy
from scipy.interpolate import PchipInterpolator

from continuity_common import (
    DEFAULT_QA, boolean, now, readcsv, readjson, sha, writecsv, writejson,
)

VERSION = "1.1.0"
MAX_GAP = 30
MAX_FRACTION = 0.10
LOCAL_EQ_TOL = 1e-10

DATASET_FIELDS = [
    "site_no", "date", "groundwater_depth_m", "latitude", "longitude",
    "observed_flag", "interpolated_flag",
]

META_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id", "origin_gap30",
    "date_min", "date_max", "duration_years_approx",
    "latitude", "longitude", "coordinate_source",
    "n_distinct_daily_coordinate_pairs",
    "n_source_values", "n_interpolated_values", "n_final_daily_values",
    "interpolated_fraction_final", "n_real_gaps", "max_gap_missing_days",
    "n_gaps_without_symmetric_3x3_context",
    "source_estimated_days_retained", "jump_alert_days_retained",
    "reserve_status", "final_status",
    "series_file", "series_sha256",
]

GAP_FIELDS = [
    "monitoring_location_id", "site_no", "segment_id", "gap_id",
    "left_date", "right_date", "start_missing_date", "end_missing_date",
    "missing_days", "left_source_support", "right_source_support",
    "symmetric_3x3_context_available",
    "local_global_max_abs_diff_m",
]

EXCLUDED_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id",
    "reserve_status", "final_status", "max_reserved_rmse_m",
]


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--carpeta", type=Path, default=DEFAULT_QA)
    p.add_argument("--experimento", type=Path)
    p.add_argument("--reserva", type=Path)
    return p


def require_file_hash(path: Path, expected: str | None, message: str):
    if not expected or sha(path) != expected:
        raise ValueError(message)


def locate(args):
    qa = args.carpeta.expanduser().resolve()
    ex = args.experimento.expanduser().resolve() if args.experimento else Path(
        (qa/"07_continuidad_exploratoria"/"ultimo_experimento.txt").read_text().strip()
    ).resolve()

    ex_manifest = readjson(ex/"manifiesto.json")
    if ex_manifest["carpeta_qa"] != str(qa):
        raise ValueError("El experimento no pertenece a la carpeta QA indicada")

    reserve = args.reserva.expanduser().resolve() if args.reserva else Path(
        (ex/"08_validacion_reserva_final_v2"/"ultima_reserva.txt").read_text().strip()
    ).resolve()
    if not reserve.is_relative_to(ex):
        raise ValueError("La reserva no pertenece al experimento")

    rsum = readjson(reserve/"resumen_reserva.json")
    if not rsum.get("etapa_completa"):
        raise ValueError("La reserva no esta cerrada/completa")
    if rsum.get("reserve_opened") is not True:
        raise ValueError("La reserva no aparece abierta")
    if rsum.get("reserved_values_evaluated") is not True:
        raise ValueError("Los valores reservados no aparecen evaluados")
    if rsum.get("real_gaps_interpolated") is not False:
        raise ValueError("Los huecos reales ya aparecen interpolados")
    if int(rsum.get("technical_errors", -1)) != 0:
        raise ValueError("La reserva contiene errores tecnicos")
    n_accepted = int(rsum.get("n_wells_final_accepted", -1))
    n_to_interpolate = int(rsum.get("n_final_to_interpolate", -1))
    n_without_interpolation = int(rsum.get("n_final_without_interpolation", -1))
    if min(n_accepted, n_to_interpolate, n_without_interpolation) < 0:
        raise ValueError("Reserve summary is missing required final counts")
    if n_to_interpolate + n_without_interpolation != n_accepted:
        raise ValueError("Reserve final counts do not reconcile")

    for name, digest in rsum.get("output_hashes", {}).items():
        p = reserve/name
        if not p.exists() or sha(p) != digest:
            raise ValueError(f"Archivo de reserva modificado: {name}")

    sel = Path(
        (ex/"07_seleccion_pre_reserva_v2"/"ultima_seleccion.txt").read_text().strip()
    ).resolve()
    if not sel.is_relative_to(ex):
        raise ValueError("Seleccion pre-reserva fuera del experimento")
    pre_summary_path = sel/"resumen_pre_reserva.json"
    pre = readjson(pre_summary_path)
    for name, digest in pre.get("output_hashes", {}).items():
        p = sel/name
        if not p.exists() or sha(p) != digest:
            raise ValueError(f"Archivo pre-reserva modificado: {name}")

    reserve_inputs = rsum.get("inputs", {})
    require_file_hash(
        pre_summary_path, reserve_inputs.get("pre_reserve_summary"),
        "La seleccion apuntada no es la misma que fue validada en reserva",
    )
    selected_path = sel/"segmentos_seleccionados_pre_reserva.csv"
    require_file_hash(
        selected_path, reserve_inputs.get("selected_segments"),
        "La tabla de segmentos seleccionados no coincide con la usada en reserva",
    )
    reserve_manifest_path = sel/"manifiesto_ventanas_reserva_max.csv"
    require_file_hash(
        reserve_manifest_path, reserve_inputs.get("reserve_manifest_max"),
        "El manifiesto de mascaras reservadas no coincide con el usado en reserva",
    )

    audit = Path(
        (ex/"05_validacion_segmentos_v2"/"ultima_auditoria.txt").read_text().strip()
    ).resolve()
    if not audit.is_relative_to(ex):
        raise ValueError("Auditoria fuera del experimento")
    audit_summary_path = audit/"resumen.json"
    audit_hash = sha(audit_summary_path)
    if audit_hash != pre["inputs"]["audit_summary"]:
        raise ValueError("La auditoria no coincide con la seleccion usada")
    require_file_hash(
        audit_summary_path, reserve_inputs.get("audit_summary"),
        "La auditoria no coincide con la cadena validada en reserva",
    )

    well_inventory = qa/"03_libres"/"pozos_libres_candidatos.csv"
    if not well_inventory.exists():
        raise ValueError("No existe 03_libres/pozos_libres_candidatos.csv")
    require_file_hash(
        well_inventory, ex_manifest.get("inventory_sha256"),
        "El inventario canonico no coincide con el usado para preparar el experimento",
    )

    return qa, ex, audit, sel, reserve, pre, rsum, well_inventory


def gzip_writer(path: Path, fields: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+".tmp")
    raw = tmp.open("wb")
    gz = gzip.GzipFile(
        filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=3
    )
    txt = io.TextIOWrapper(gz, encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(txt, fieldnames=fields, extrasaction="raise")
    writer.writeheader()
    return tmp, raw, gz, txt, writer


def close_gzip_atomic(path: Path, handles):
    tmp, raw, gz, txt, writer = handles
    txt.flush()
    txt.close()
    if not raw.closed:
        raw.close()
    tmp.replace(path)


def fmt_interp(v: float) -> str:
    if not math.isfinite(v):
        raise ValueError("Valor interpolado no finito")
    return format(float(v), ".12g")


def read_segment(audit: Path, s: dict) -> list[dict]:
    p = (audit/s["segment_file_v2"]).resolve()
    if not p.is_relative_to(audit):
        raise ValueError(f"Ruta fuera de auditoria: {s['segment_id']}")
    if sha(p) != s["segment_sha256_v2"]:
        raise ValueError(f"Hash de segmento modificado: {s['segment_id']}")
    rows = list(readcsv(p))
    if not rows:
        raise ValueError(f"Segmento vacio: {s['segment_id']}")
    return rows


def canonical_coordinates(well: dict, sid: str):
    try:
        lat = float(well["latitude"])
        lon = float(well["longitude"])
    except (KeyError, ValueError):
        raise ValueError(f"{sid}: coordenadas canonicas ausentes/invalidas") from None

    if not (
        math.isfinite(lat) and math.isfinite(lon)
        and -90 <= lat <= 90 and -180 <= lon <= 180
    ):
        raise ValueError(f"{sid}: coordenadas canonicas fuera de rango")

    return well["latitude"], well["longitude"]


def daily_coordinate_diagnostic(rows: list[dict]) -> int:
    pairs = set()
    for r in rows:
        lat_s = r.get("latitude", "").strip()
        lon_s = r.get("longitude", "").strip()
        if not lat_s or not lon_s:
            continue
        try:
            lat = float(lat_s)
            lon = float(lon_s)
        except ValueError:
            continue
        if (
            math.isfinite(lat) and math.isfinite(lon)
            and -90 <= lat <= 90 and -180 <= lon <= 180
        ):
            pairs.add((lat_s, lon_s))
    return len(pairs)


def gap_records(x: np.ndarray, y: np.ndarray, mid: str, site_no: str,
                sid: str, global_interp: PchipInterpolator):
    out = []
    gid = 0

    for j in range(1, len(x)):
        missing = int(x[j] - x[j-1] - 1)
        if missing <= 0:
            continue

        gid += 1
        if missing > MAX_GAP:
            raise ValueError(f"{sid}: hueco real {missing} > {MAX_GAP} dias")

        left_support = j
        right_support = len(x) - j
        if left_support < 1 or right_support < 1:
            raise ValueError(f"{sid}: hueco no interno")

        xt = np.arange(int(x[j-1])+1, int(x[j]), dtype=float)
        pred_global = np.asarray(global_interp(xt), dtype=float)

        lo = max(0, j-2)
        hi = min(len(x), j+2)

        if hi-lo < 3:
            if lo == 0:
                hi = min(len(x), 3)
            if hi == len(x):
                lo = max(0, len(x)-3)

        if hi-lo < 3:
            raise ValueError(f"{sid}: insuficientes puntos para PCHIP local")

        local_interp = PchipInterpolator(
            x[lo:hi], y[lo:hi], extrapolate=False
        )
        pred_local = np.asarray(local_interp(xt), dtype=float)

        if not np.isfinite(pred_global).all() or not np.isfinite(pred_local).all():
            raise ValueError(f"{sid}: prediccion no finita en hueco {gid}")

        diff = float(np.max(np.abs(pred_global-pred_local))) if len(xt) else 0.0
        if diff > LOCAL_EQ_TOL:
            raise ValueError(
                f"{sid}: PCHIP global/local difieren {diff:.3e} m "
                f"en hueco {gid}"
            )

        left_d = date.fromordinal(int(x[j-1]))
        right_d = date.fromordinal(int(x[j]))

        out.append(dict(
            monitoring_location_id=mid,
            site_no=site_no,
            segment_id=sid,
            gap_id=gid,
            left_date=left_d.isoformat(),
            right_date=right_d.isoformat(),
            start_missing_date=date.fromordinal(int(x[j-1])+1).isoformat(),
            end_missing_date=date.fromordinal(int(x[j])-1).isoformat(),
            missing_days=missing,
            left_source_support=left_support,
            right_source_support=right_support,
            symmetric_3x3_context_available=int(
                left_support >= 3 and right_support >= 3
            ),
            local_global_max_abs_diff_m=diff,
        ))

    return out


def process_well(audit: Path, root: Path, s: dict,
                 final_state: dict, well: dict):
    rows = read_segment(audit, s)
    sid = s["segment_id"]
    mid = s["monitoring_location_id"]
    site_no = s["site_no"]

    if well["monitoring_location_id"] != mid or well["site_no"] != site_no:
        raise ValueError(f"{sid}: inventario canonico no concilia con el segmento")

    if any(not boolean(r["usable_input"]) for r in rows):
        raise ValueError(f"{sid}: contiene filas no utilizables")
    if any(boolean(r["interpolated_flag"]) for r in rows):
        raise ValueError(f"{sid}: ya contiene interpolacion previa")

    rows.sort(key=lambda r: r["date"])
    dates = [date.fromisoformat(r["date"]) for r in rows]
    if len(set(dates)) != len(dates):
        raise ValueError(f"{sid}: fechas duplicadas")
    if (
        dates[0].isoformat() != s["date_min"]
        or dates[-1].isoformat() != s["date_max"]
    ):
        raise ValueError(f"{sid}: limites temporales no concilian")

    lat_s, lon_s = canonical_coordinates(well, sid)
    n_daily_coord_pairs = daily_coordinate_diagnostic(rows)

    x = np.asarray([d.toordinal() for d in dates], dtype=float)
    y = np.asarray([float(r["depth_to_water_m"]) for r in rows], dtype=float)

    if not np.isfinite(y).all():
        raise ValueError(f"{sid}: valores fuente no finitos")
    if not np.all(np.diff(x) > 0):
        raise ValueError(f"{sid}: fechas no estrictamente crecientes")

    expected = int(x[-1]-x[0]+1)
    missing_expected = expected-len(rows)

    if expected != int(s["expected_days"]):
        raise ValueError(f"{sid}: expected_days no concilia")
    if missing_expected != int(s["missing_days"]):
        raise ValueError(f"{sid}: missing_days no concilia")
    if missing_expected != int(final_state["missing_days"]):
        raise ValueError(f"{sid}: missing_days difiere de estado final")

    frac = missing_expected/expected
    if frac > MAX_FRACTION + 1e-15:
        raise ValueError(
            f"{sid}: fraccion a interpolar {frac:.6f} > {MAX_FRACTION}"
        )

    by_ord = {d.toordinal(): r for d, r in zip(dates, rows)}

    if missing_expected:
        interp = PchipInterpolator(x, y, extrapolate=False)
        gaps = gap_records(x, y, mid, site_no, sid, interp)

        if sum(int(g["missing_days"]) for g in gaps) != missing_expected:
            raise ValueError(f"{sid}: suma de huecos no concilia")
        if max(int(g["missing_days"]) for g in gaps) != int(
            s["max_gap_missing_days"]
        ):
            raise ValueError(f"{sid}: max_gap no concilia")
    else:
        interp = None
        gaps = []
        if int(s["max_gap_missing_days"]) != 0:
            raise ValueError(f"{sid}: sin huecos pero max_gap != 0")

    series_rel = Path("series")/s["estado"]/(mid+".csv.gz")
    series_path = root/series_rel
    handles = gzip_writer(series_path, DATASET_FIELDS)
    writer = handles[-1]

    n_source = n_interp = 0
    first_ord = int(x[0])
    last_ord = int(x[-1])

    for ordv in range(first_ord, last_ord+1):
        d = date.fromordinal(ordv).isoformat()

        if ordv in by_ord:
            r = by_ord[ordv]
            value = r["depth_to_water_m"]
            observed_flag = 1
            interpolated_flag = 0
            n_source += 1
        else:
            value = fmt_interp(float(interp(float(ordv))))
            observed_flag = 0
            interpolated_flag = 1
            n_interp += 1

        writer.writerow(dict(
            site_no=site_no,
            date=d,
            groundwater_depth_m=value,
            latitude=lat_s,
            longitude=lon_s,
            observed_flag=observed_flag,
            interpolated_flag=interpolated_flag,
        ))

    close_gzip_atomic(series_path, handles)

    if n_source != len(rows) or n_interp != missing_expected:
        raise ValueError(f"{sid}: conteos finales no concilian")

    source_est = sum(
        boolean(r["source_estimated_effective"]) for r in rows
    )
    jump_days = sum(boolean(r["jump_alert"]) for r in rows)
    no_3x3 = sum(
        not bool(int(g["symmetric_3x3_context_available"]))
        for g in gaps
    )

    meta = dict(
        monitoring_location_id=mid,
        site_no=site_no,
        estado=s["estado"],
        segment_id=sid,
        origin_gap30=s["origin_gap30"],
        date_min=s["date_min"],
        date_max=s["date_max"],
        duration_years_approx=s["duration_years_approx"],
        latitude=lat_s,
        longitude=lon_s,
        coordinate_source="03_libres/pozos_libres_candidatos.csv",
        n_distinct_daily_coordinate_pairs=n_daily_coord_pairs,
        n_source_values=n_source,
        n_interpolated_values=n_interp,
        n_final_daily_values=n_source+n_interp,
        interpolated_fraction_final=n_interp/(n_source+n_interp),
        n_real_gaps=len(gaps),
        max_gap_missing_days=s["max_gap_missing_days"],
        n_gaps_without_symmetric_3x3_context=no_3x3,
        source_estimated_days_retained=source_est,
        jump_alert_days_retained=jump_days,
        reserve_status=final_state["reserve_status"],
        final_status=final_state["final_status"],
        series_file=str(series_rel),
        series_sha256=sha(series_path),
    )

    return meta, gaps


def ratio_or_none(numerator: int | float, denominator: int | float):
    return numerator / denominator if denominator else None


def aggregate_or_none(values, fn):
    return fn(values) if values else None


def main():
    args = parser().parse_args()
    qa, ex, audit, sel, reserve, pre, rsum, well_inventory_path = locate(args)

    selected = list(readcsv(sel/"segmentos_seleccionados_pre_reserva.csv"))
    final_states = list(readcsv(reserve/"estado_final_pre_interpolacion.csv"))
    well_rows = list(readcsv(well_inventory_path))

    selected_by_sid = {r["segment_id"]: r for r in selected}
    final_by_sid = {r["segment_id"]: r for r in final_states}
    wells = {r["monitoring_location_id"]: r for r in well_rows}

    if len(wells) != len(well_rows):
        raise ValueError("Inventario canonico contiene monitoring_location_id duplicado")
    if set(selected_by_sid) != set(final_by_sid):
        raise ValueError("Seleccion pre-reserva y estado final no concilian")

    accepted_statuses = {
        "aceptado_para_interpolacion",
        "aceptado_sin_interpolacion",
    }
    accepted_states = [
        r for r in final_states if r["final_status"] in accepted_statuses
    ]
    excluded_states = [
        r for r in final_states if r["final_status"] not in accepted_statuses
    ]

    if len(accepted_states) != int(rsum["n_wells_final_accepted"]):
        raise ValueError("Accepted-state count does not match the reserve summary")
    if len(excluded_states) != int(rsum["n_wells_final_excluded"]):
        raise ValueError("Excluded-state count does not match the reserve summary")
    if len(accepted_states) + len(excluded_states) != len(final_states):
        raise ValueError("Final reserve states do not reconcile")

    missing_wells = {
        r["monitoring_location_id"] for r in accepted_states
    } - set(wells)
    if missing_wells:
        raise ValueError(
            f"Faltan {len(missing_wells)} pozos aceptados en inventario canonico"
        )

    spec = dict(
        version=VERSION,
        method="pchip",
        interpolation_support="all_retained_source_values_within_segment",
        validation_mask_context="3_source_values_each_side",
        natural_gap_3x3_required=False,
        max_gap_days=MAX_GAP,
        max_interpolated_fraction=MAX_FRACTION,
        extrapolation=False,
        preserve_source_values=True,
        coordinate_source="03_libres/pozos_libres_candidatos.csv",
        local_global_equivalence_tolerance_m=LOCAL_EQ_TOL,
        no_post_reserve_fallback=True,
        final_schema=DATASET_FIELDS,
        inputs={
            "reserve_summary": sha(reserve/"resumen_reserva.json"),
            "final_state": sha(reserve/"estado_final_pre_interpolacion.csv"),
            "selected_segments": sha(
                sel/"segmentos_seleccionados_pre_reserva.csv"
            ),
            "audit_summary": sha(audit/"resumen.json"),
            "canonical_well_inventory": sha(well_inventory_path),
        },
        script_sha256=sha(Path(__file__)),
        python=platform.python_version(),
        numpy=np.__version__,
        scipy=scipy.__version__,
    )

    token = hashlib.sha256(
        json.dumps(spec, sort_keys=True).encode()
    ).hexdigest()[:12]

    root = ex/"09_dataset_final_v2"/("dataset_"+token)
    root.mkdir(parents=True, exist_ok=True)

    writejson(root/"resumen_dataset_final.json", dict(
        etapa_completa=False,
        inicio_utc=now(),
        **spec,
    ))

    excluded_rows = [
        dict(
            monitoring_location_id=r["monitoring_location_id"],
            site_no=r["site_no"],
            estado=r["estado"],
            segment_id=r["segment_id"],
            reserve_status=r["reserve_status"],
            final_status=r["final_status"],
            max_reserved_rmse_m=r["max_reserved_rmse_m"],
        )
        for r in excluded_states
    ]
    writecsv(
        root/"pozos_excluidos_tras_reserva.csv",
        excluded_rows,
        EXCLUDED_FIELDS,
    )

    metadata = []
    all_gaps = []
    errors = []

    accepted_states.sort(
        key=lambda r: (r["estado"], r["site_no"], r["segment_id"])
    )

    for i, fstate in enumerate(accepted_states, 1):
        sid = fstate["segment_id"]
        s = selected_by_sid[sid]
        mid = fstate["monitoring_location_id"]

        try:
            meta, gaps = process_well(
                audit, root, s, fstate, wells[mid]
            )
            metadata.append(meta)
            all_gaps.extend(gaps)
        except Exception as exc:
            errors.append(dict(
                monitoring_location_id=mid,
                segment_id=sid,
                error=f"{type(exc).__name__}: {exc}",
            ))

        if i % 20 == 0 or i == len(accepted_states):
            print(
                f"Interpolacion/exportacion: {i}/{len(accepted_states)}; "
                f"errores tecnicos {len(errors)}",
                flush=True,
            )

    writecsv(
        root/"errores_tecnicos.csv",
        errors,
        ["monitoring_location_id", "segment_id", "error"],
    )

    if errors:
        writejson(root/"resumen_dataset_final.json", dict(
            etapa_completa=False,
            finalizado_utc=now(),
            errores_tecnicos=len(errors),
            **spec,
        ))
        raise ValueError(
            f"Hay {len(errors)} errores tecnicos. No se construyo el combinado "
            "final. Revisar errores_tecnicos.csv."
        )

    if len(metadata) != len(accepted_states):
        raise ValueError("Final metadata count does not match accepted reserve states")

    writecsv(root/"metadata_pozos_finales.csv", metadata, META_FIELDS)
    writecsv(root/"huecos_interpolados.csv", all_gaps, GAP_FIELDS)

    combined = root/"dataset_groundwater_daily_final.csv.gz"
    handles = gzip_writer(combined, DATASET_FIELDS)
    combined_writer = handles[-1]
    total_rows_streamed = 0

    for m in sorted(metadata, key=lambda r: (r["site_no"], r["date_min"])):
        p = (root/m["series_file"]).resolve()
        if not p.is_relative_to(root) or sha(p) != m["series_sha256"]:
            raise ValueError(
                f"Serie final modificada antes de combinar: {m['site_no']}"
            )

        for row in readcsv(p):
            combined_writer.writerow({k: row[k] for k in DATASET_FIELDS})
            total_rows_streamed += 1

    close_gzip_atomic(combined, handles)

    total_source = sum(int(m["n_source_values"]) for m in metadata)
    total_interp = sum(int(m["n_interpolated_values"]) for m in metadata)
    total_final = sum(int(m["n_final_daily_values"]) for m in metadata)

    if total_source + total_interp != total_final:
        raise ValueError("Conteos globales fuente + interpolado no concilian")
    if total_rows_streamed != total_final:
        raise ValueError("El dataset combinado no tiene el total esperado")
    if sum(int(g["missing_days"]) for g in all_gaps) != total_interp:
        raise ValueError(
            "La suma de huecos no coincide con valores interpolados"
        )

    durations = [float(m["duration_years_approx"]) for m in metadata]
    fracs = [float(m["interpolated_fraction_final"]) for m in metadata]
    date_mins = [m["date_min"] for m in metadata]
    date_maxs = [m["date_max"] for m in metadata]
    origins = Counter(m["origin_gap30"] for m in metadata)

    recovered = [
        m for m in metadata
        if m["origin_gap30"] == "recuperado_segmentacion"
    ]
    direct = [
        m for m in metadata
        if m["origin_gap30"] == "directo"
    ]

    summary_core = dict(
        etapa_completa=True,
        n_final_wells=len(metadata),
        n_direct_final=len(direct),
        n_recovered_by_segmentation_final=len(recovered),
        n_final_without_interpolation=sum(
            int(m["n_interpolated_values"]) == 0 for m in metadata
        ),
        n_final_with_interpolation=sum(
            int(m["n_interpolated_values"]) > 0 for m in metadata
        ),
        n_source_values_retained=total_source,
        n_interpolated_values_created=total_interp,
        n_final_daily_values=total_final,
        global_interpolated_fraction=ratio_or_none(total_interp, total_final),
        n_real_gaps_interpolated=len(all_gaps),
        n_real_gaps_without_symmetric_3x3_context=sum(
            not bool(int(g["symmetric_3x3_context_available"]))
            for g in all_gaps
        ),
        max_real_gap_days=max(
            (int(g["missing_days"]) for g in all_gaps),
            default=0,
        ),
        temporal_coverage_min=aggregate_or_none(date_mins, min),
        temporal_coverage_max=aggregate_or_none(date_maxs, max),
        median_segment_duration_years=aggregate_or_none(durations, median),
        mean_segment_duration_years=aggregate_or_none(durations, mean),
        min_segment_duration_years=aggregate_or_none(durations, min),
        max_segment_duration_years=aggregate_or_none(durations, max),
        median_interpolated_fraction=aggregate_or_none(fracs, median),
        mean_interpolated_fraction=aggregate_or_none(fracs, mean),
        max_interpolated_fraction_observed=aggregate_or_none(fracs, max),
        source_estimated_days_retained=sum(
            int(m["source_estimated_days_retained"])
            for m in metadata
        ),
        jump_alert_days_retained=sum(
            int(m["jump_alert_days_retained"])
            for m in metadata
        ),
        wells_with_multiple_daily_coordinate_pairs=sum(
            int(m["n_distinct_daily_coordinate_pairs"]) > 1
            for m in metadata
        ),
        recovered_source_values_final=sum(
            int(m["n_source_values"]) for m in recovered
        ),
        recovered_final_daily_values=sum(
            int(m["n_final_daily_values"]) for m in recovered
        ),
        recovered_interpolated_values=sum(
            int(m["n_interpolated_values"]) for m in recovered
        ),
        origins=dict(sorted(origins.items())),
        excluded_after_reserve=len(excluded_states),
        no_post_reserve_fallback=True,
    )

    outputs = [
        root/"metadata_pozos_finales.csv",
        root/"huecos_interpolados.csv",
        root/"pozos_excluidos_tras_reserva.csv",
        root/"errores_tecnicos.csv",
        combined,
    ] + [root/m["series_file"] for m in metadata]

    # Build the final summary without duplicate-key expansion. The historical
    # run required a separate administrative close script after this point;
    # this public version integrates that non-scientific repair directly.
    final_summary = dict(spec)
    final_summary.update(summary_core)
    final_summary.update(
        finalizado_utc=now(),
        output_hashes={
            str(p.relative_to(root)): sha(p)
            for p in outputs
        },
        note=(
            "Final daily dataset built from wells accepted after reserved validation. "
            "The 3+3 rule is retained for validation masks but is not an additional "
            "exclusion rule for natural internal gaps. PCHIP is applied to all retained "
            "source values within each segment without extrapolation."
        ),
    )
    writejson(root/"resumen_dataset_final.json", final_summary)

    pointer = ex/"09_dataset_final_v2"/"ultimo_dataset.txt"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    tmp = pointer.with_suffix(".tmp")
    tmp.write_text(str(root)+"\n", encoding="utf-8")
    tmp.replace(pointer)

    print("\nDATASET FINAL CONSTRUIDO")
    print(json.dumps(summary_core, indent=2, ensure_ascii=False))
    print("Resultados:", root)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nInterrumpido. La salida queda incompleta; puede repetirse "
            "el mismo comando.",
            file=sys.stderr,
        )
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
