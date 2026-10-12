#!/usr/bin/env python3
"""Select one segment per well after development validation and freeze the reserved masking panel before reserved values are evaluated."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median

from continuity_common import (
    DEFAULT_QA, boolean, now, readcsv, readjson, require_stage, sha, writecsv,
    writejson,
)

VERSION = "2.0.0"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "validation.json"
BASE_LENGTHS = (1, 3, 7, 15, 30)
BUDGETS = (10, 20, 30, 50)

DECISION_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id",
    "structural_rank_all", "development_usable", "development_status",
    "all_required_lengths_pass", "n_required_lengths", "max_required_rmse_m",
    "duration_years_approx", "date_min", "date_max", "elapsed_days",
    "completeness", "n_days", "truth_days", "expected_days", "missing_days",
    "fraction_to_interpolate", "max_gap_missing_days", "origin_gap30",
    "selected_pre_reserve", "selection_reason",
]

SELECTED_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id",
    "origin_gap30", "structural_rank_all", "n_candidates_well",
    "n_development_usable_well", "fallback_used",
    "development_status", "duration_years_approx", "date_min", "date_max",
    "elapsed_days", "completeness", "n_days", "truth_days", "expected_days",
    "missing_days", "fraction_to_interpolate", "max_gap_missing_days",
    "segment_file_v2", "segment_sha256_v2",
]

RECOVERED_FIELDS = SELECTED_FIELDS

RESERVE_WINDOW_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id",
    "gap_length_days", "mask_id", "start_date", "end_date",
    "guard_left_start", "guard_left_end", "guard_right_start",
    "guard_right_end", "year", "quarter", "segment_zone", "phase",
    "selection_rank",
]

RESERVE_AUDIT_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id",
    "duration_years_approx", "max_gap_missing_days", "gap_length_days",
    "n_candidate_windows_raw", "n_nonoverlap_capacity", "n_selected_panel",
    "n_years_selected", "n_quarters_selected", "n_zones_selected",
    "can_10", "can_20", "can_30", "can_50", "coverage_note",
]


def phase(mid: str, year: int) -> str:
    offset = int(hashlib.sha256(mid.encode()).hexdigest()[:8], 16) % 5
    return "reservada" if year % 5 == offset else "desarrollo"


def stable_key(seed: int, *parts: object) -> str:
    return hashlib.sha256(
        "|".join(map(str, (seed,) + parts)).encode()
    ).hexdigest()


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
    if not math.isclose(float(c.get("stability_tolerance_m", -1)), 0.02):
        raise ValueError("stability_tolerance_m debe permanecer en 0.02 m")
    if not math.isclose(float(c.get("rmse_threshold_m", -1)), 0.20):
        raise ValueError("rmse_threshold_m debe permanecer en 0.20 m")
    if not math.isclose(float(c.get("max_interpolated_fraction", -1)), 0.10):
        raise ValueError("max_interpolated_fraction debe permanecer en 0.10")
    if c.get("method") != "pchip":
        raise ValueError("method debe permanecer en pchip")
    if c.get("reserved_evaluated") is not False:
        raise ValueError("La configuracion no debe declarar la reserva evaluada")
    return c


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--carpeta", type=Path, default=DEFAULT_QA)
    p.add_argument("--experimento", type=Path)
    p.add_argument("--auditoria", type=Path)
    p.add_argument("--desarrollo", type=Path)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return p


def locate(args):
    qa = args.carpeta.expanduser().resolve()
    ex = args.experimento.expanduser().resolve() if args.experimento else Path(
        (qa / "07_continuidad_exploratoria" / "ultimo_experimento.txt")
        .read_text().strip()
    ).resolve()

    manifest = readjson(ex / "manifiesto.json")
    if manifest["carpeta_qa"] != str(qa):
        raise ValueError("El experimento no pertenece a la carpeta QA indicada")

    audit = args.auditoria.expanduser().resolve() if args.auditoria else Path(
        (ex / "05_validacion_segmentos_v2" / "ultima_auditoria.txt")
        .read_text().strip()
    ).resolve()
    dev = args.desarrollo.expanduser().resolve() if args.desarrollo else Path(
        (ex / "06_desarrollo_todos_segmentos_v2" / "ultimo_desarrollo.txt")
        .read_text().strip()
    ).resolve()

    if not audit.is_relative_to(ex) or not dev.is_relative_to(ex):
        raise ValueError("Auditoria/desarrollo fuera del experimento")

    asum = readjson(audit / "resumen.json")
    dsum = readjson(dev / "resumen.json")

    if not asum.get("etapa_completa") or asum.get("reserved_evaluated") is not False:
        raise ValueError("Auditoria incompleta o reserva alterada")
    if (not dsum.get("etapa_completa")
            or dsum.get("reserved_evaluated") is not False
            or dsum.get("final_validation") is not False):
        raise ValueError("Desarrollo incompleto o reserva/final ya evaluada")

    for name, digest in asum.get("output_hashes", {}).items():
        p = audit / name
        if not p.exists() or sha(p) != digest:
            raise ValueError(f"Archivo de auditoria modificado: {name}")

    for name, digest in dsum.get("output_hashes", {}).items():
        p = dev / name
        if not p.exists() or sha(p) != digest:
            raise ValueError(f"Archivo de desarrollo modificado: {name}")

    expected_audit_sha = dsum.get("inputs", {}).get("audit_summary")
    if expected_audit_sha != sha(audit / "resumen.json"):
        raise ValueError("El desarrollo no corresponde a la auditoria actual")

    require_stage(ex, "02_segmentacion")
    return qa, ex, audit, dev, asum, dsum


def relevant_lengths(gmax: int) -> list[int]:
    if gmax <= 0:
        return []
    if gmax > 30:
        raise ValueError(f"Gmax fuera del escenario 30 d: {gmax}")
    return sorted(set([x for x in BASE_LENGTHS if x <= gmax] + [gmax]))


def zone_for(d: date, first: date, last: date) -> str:
    span = max(1, (last - first).days)
    f = (d - first).days / span
    return "inicio" if f < 1/3 else "centro" if f < 2/3 else "final"


def enumerate_candidates_reserve(rows: list[dict], mid: str, first: date,
                                 last: date, length: int,
                                 guard: int) -> list[dict]:
    by_date = {date.fromisoformat(r["date"]): r for r in rows}
    candidates = []
    start = first + timedelta(days=guard)
    latest = last - timedelta(days=length + guard - 1)

    while start <= latest:
        end = start + timedelta(days=length - 1)
        left0 = start - timedelta(days=guard)
        left1 = start - timedelta(days=1)
        right0 = end + timedelta(days=1)
        right1 = end + timedelta(days=guard)
        full = [
            left0 + timedelta(days=i)
            for i in range((right1 - left0).days + 1)
        ]

        same_year = all(d.year == start.year for d in full)
        if same_year and phase(mid, start.year) == "reservada":
            recs = [by_date.get(d) for d in full]
            if all(r is not None and boolean(r["eligible_truth"]) for r in recs):
                candidates.append(dict(
                    start=start, end=end,
                    left0=left0, left1=left1,
                    right0=right0, right1=right1,
                    year=start.year,
                    quarter=(start.month - 1) // 3 + 1,
                    zone=zone_for(start, first, last),
                ))
        start += timedelta(days=1)

    return candidates


def max_nonoverlap(candidates: list[dict]) -> list[dict]:
    chosen = []
    last_end = None
    for x in sorted(candidates, key=lambda r: (r["end"], r["start"])):
        if last_end is None or x["start"] > last_end:
            chosen.append(x)
            last_end = x["end"]
    return chosen


def balanced_ranked_panel(maximal: list[dict], maximum: int, seed: int,
                          mid: str, sid: str, length: int) -> list[dict]:
    groups = defaultdict(list)
    for x in maximal:
        groups[(x["year"], x["quarter"], x["zone"])].append(x)

    for key, vals in groups.items():
        vals.sort(key=lambda x: stable_key(
            seed, mid, sid, "reserve", length, key, x["start"].isoformat()
        ))

    keys = sorted(
        groups,
        key=lambda k: stable_key(seed, mid, sid, "reserve_group", length, *k)
    )

    out = []
    i = 0
    target = min(maximum, len(maximal))
    while len(out) < target:
        progress = False
        for key in keys:
            if i < len(groups[key]):
                out.append(groups[key][i])
                progress = True
                if len(out) == target:
                    break
        if not progress:
            break
        i += 1
    return out


def structural_key(s: dict):
    return (
        -int(s["elapsed_days"]),
        -float(s["completeness"]),
        s["date_min"],
        s["segment_id"],
    )


def optional_float(value):
    return None if value in ("", None) else float(value)


def main() -> None:
    args = parser().parse_args()
    c = configuration(args.config.resolve())
    qa, ex, audit, dev, audit_summary, dev_summary = locate(args)
    if audit_summary.get("config") != c:
        raise ValueError("La seleccion no usa la misma configuracion que la auditoria de mascaras")
    if dev_summary.get("config") != c:
        raise ValueError("La seleccion no usa la misma configuracion que la validacion de desarrollo")

    segs = list(readcsv(audit / "segmentos_elegibles_30d.csv"))
    status_rows = list(readcsv(dev / "estado_segmentos.csv"))
    metric_rows = list(readcsv(dev / "metricas_segmento_longitud.csv"))

    if len({r["segment_id"] for r in segs}) != len(segs):
        raise ValueError("Duplicate segment_id values in the eligible-segment inventory")

    seg_by_sid = {}
    for s in segs:
        sid = s["segment_id"]
        if sid in seg_by_sid:
            raise ValueError(f"segment_id duplicado: {sid}")
        seg_by_sid[sid] = s

    status_by_sid = {}
    for r in status_rows:
        sid = r["segment_id"]
        if sid in status_by_sid:
            raise ValueError(f"Estado duplicado para {sid}")
        status_by_sid[sid] = r

    if set(status_by_sid) != set(seg_by_sid):
        raise ValueError("estado_segmentos.csv no concilia con el inventario de segmentos elegibles")

    metrics_by_sid = defaultdict(list)
    for r in metric_rows:
        if r["segment_id"] not in seg_by_sid:
            raise ValueError(f"Metrica para segmento desconocido: {r['segment_id']}")
        metrics_by_sid[r["segment_id"]].append(r)

    state30 = [
        r for r in readcsv(ex / "02_segmentacion" / "estado_por_pozo.csv")
        if int(r["gap_scenario"]) == 30
    ]
    origin_by_well = {r["monitoring_location_id"]: r["origin"] for r in state30}

    evaluation = {}
    for sid, s in seg_by_sid.items():
        st = status_by_sid[sid]["status"]
        gmax = int(s["max_gap_missing_days"])
        required = relevant_lengths(gmax)
        max_rmse = None
        all_pass = False

        if st == "sin_interpolacion_necesaria":
            if int(s["missing_days"]) != 0 or gmax != 0:
                raise ValueError(f"{sid}: declarado sin interpolacion pero tiene huecos")
            if metrics_by_sid.get(sid):
                raise ValueError(f"{sid}: segmento sin huecos no debe tener metricas de masking")
            dev_status = "sin_huecos_aceptado"
            usable = True
            all_pass = True

        elif st == "evaluado_desarrollo":
            rows = metrics_by_sid.get(sid, [])
            lengths_found = sorted(int(r["gap_length_days"]) for r in rows)
            if lengths_found != required:
                raise ValueError(
                    f"{sid}: longitudes {lengths_found} != requeridas {required}"
                )
            if any(int(r["n_masks"]) != 50 for r in rows):
                raise ValueError(f"{sid}: no todas las longitudes tienen 50 mascaras")
            if any(
                (float(r["rmse_m"]) <= float(c["rmse_threshold_m"]))
                != bool(int(r["rmse_le_0_20"]))
                for r in rows
            ):
                raise ValueError(f"{sid}: bandera RMSE inconsistente")
            vals = [float(r["rmse_m"]) for r in rows]
            max_rmse = max(vals)
            all_pass = all(v <= float(c["rmse_threshold_m"]) for v in vals)
            frac_ok = float(s["fraction_to_interpolate"]) <= float(
                c["max_interpolated_fraction"]
            )
            usable = all_pass and frac_ok
            dev_status = "pass_desarrollo" if usable else (
                "fail_fraccion_interpolada" if not frac_ok
                else "fail_rmse_longitud"
            )
        else:
            usable = False
            dev_status = st

        evaluation[sid] = dict(
            development_usable=int(usable),
            development_status=dev_status,
            all_required_lengths_pass=int(all_pass),
            n_required_lengths=len(required),
            max_required_rmse_m="" if max_rmse is None else max_rmse,
        )

    counts_eval = Counter(v["development_status"] for v in evaluation.values())
    n_dev_usable = sum(v["development_usable"] for v in evaluation.values())
    if len(evaluation) != len(segs):
        raise ValueError("Development evaluation does not cover every eligible segment")

    by_well = defaultdict(list)
    for s in segs:
        by_well[s["monitoring_location_id"]].append(s)

    if set(by_well) != {r["monitoring_location_id"] for r in segs}:
        raise ValueError("Well grouping does not reconcile with eligible segments")

    decisions = []
    selected = []
    no_pass_wells = []

    for mid, candidates in sorted(by_well.items()):
        ordered = sorted(candidates, key=structural_key)
        rank_all = {s["segment_id"]: i for i, s in enumerate(ordered, 1)}
        usable = [s for s in ordered if evaluation[s["segment_id"]]["development_usable"]]
        chosen = usable[0] if usable else None

        if chosen is None:
            no_pass_wells.append(dict(
                monitoring_location_id=mid,
                n_candidates=len(ordered),
                reason="ningun_segmento_cumple_desarrollo",
            ))
        else:
            rank = rank_all[chosen["segment_id"]]
            selected.append(dict(
                monitoring_location_id=mid,
                site_no=chosen["site_no"],
                estado=chosen["estado"],
                segment_id=chosen["segment_id"],
                origin_gap30=origin_by_well.get(mid, ""),
                structural_rank_all=rank,
                n_candidates_well=len(ordered),
                n_development_usable_well=len(usable),
                fallback_used=int(rank > 1),
                development_status=evaluation[chosen["segment_id"]]["development_status"],
                duration_years_approx=chosen["duration_years_approx"],
                date_min=chosen["date_min"],
                date_max=chosen["date_max"],
                elapsed_days=chosen["elapsed_days"],
                completeness=chosen["completeness"],
                n_days=chosen["n_days"],
                truth_days=chosen["truth_days"],
                expected_days=chosen["expected_days"],
                missing_days=chosen["missing_days"],
                fraction_to_interpolate=chosen["fraction_to_interpolate"],
                max_gap_missing_days=chosen["max_gap_missing_days"],
                segment_file_v2=chosen["segment_file_v2"],
                segment_sha256_v2=chosen["segment_sha256_v2"],
            ))

        for s in ordered:
            sid = s["segment_id"]
            ev = evaluation[sid]
            is_selected = int(chosen is not None and sid == chosen["segment_id"])
            if is_selected:
                reason = "seleccionado_segmento_utilizable_mas_largo"
            elif ev["development_usable"]:
                reason = "utilizable_no_seleccionado_menor_prioridad_estructural"
            else:
                reason = ev["development_status"]

            decisions.append(dict(
                monitoring_location_id=mid,
                site_no=s["site_no"],
                estado=s["estado"],
                segment_id=sid,
                structural_rank_all=rank_all[sid],
                development_usable=ev["development_usable"],
                development_status=ev["development_status"],
                all_required_lengths_pass=ev["all_required_lengths_pass"],
                n_required_lengths=ev["n_required_lengths"],
                max_required_rmse_m=ev["max_required_rmse_m"],
                duration_years_approx=s["duration_years_approx"],
                date_min=s["date_min"],
                date_max=s["date_max"],
                elapsed_days=s["elapsed_days"],
                completeness=s["completeness"],
                n_days=s["n_days"],
                truth_days=s["truth_days"],
                expected_days=s["expected_days"],
                missing_days=s["missing_days"],
                fraction_to_interpolate=s["fraction_to_interpolate"],
                max_gap_missing_days=s["max_gap_missing_days"],
                origin_gap30=origin_by_well.get(mid, ""),
                selected_pre_reserve=is_selected,
                selection_reason=reason,
            ))

    if len(selected) + len(no_pass_wells) != len(by_well):
        raise ValueError(
            "Selected wells plus wells without a development-pass segment do not "
            "reconcile with the temporally eligible well inventory"
        )

    recovered_selected = [
        r for r in selected if r["origin_gap30"] == "recuperado_segmentacion"
    ]
    direct_selected = [r for r in selected if r["origin_gap30"] == "directo"]

    unknown_origin = [
        r for r in selected
        if r["origin_gap30"] not in {"directo", "recuperado_segmentacion"}
    ]
    if unknown_origin:
        raise ValueError("Hay seleccionados con origen gap30 no reconocido")

    def recovery_summary(label: str, rows: list[dict]) -> dict:
        durations = [float(r["duration_years_approx"]) for r in rows]
        n_obs = sum(int(r["n_days"]) for r in rows)
        n_truth = sum(int(r["truth_days"]) for r in rows)
        expected = sum(int(r["expected_days"]) for r in rows)
        missing = sum(int(r["missing_days"]) for r in rows)
        return dict(
            grupo=label,
            n_wells=len(rows),
            n_source_daily_values_retained=n_obs,
            n_eligible_truth_values=n_truth,
            n_calendar_days_final_grid=expected,
            n_days_to_interpolate_later=missing,
            observed_equivalent_years=n_obs / 365.25 if rows else 0,
            sum_segment_duration_years=sum(durations),
            median_segment_duration_years=median(durations) if durations else "",
            min_segment_duration_years=min(durations) if durations else "",
            max_segment_duration_years=max(durations) if durations else "",
        )

    recovery_rows = [
        recovery_summary("directo", direct_selected),
        recovery_summary("recuperado_segmentacion", recovered_selected),
        recovery_summary("total_pre_reserva", selected),
    ]

    reserve_windows = []
    reserve_audit = []

    for i, s in enumerate(selected, 1):
        if int(s["missing_days"]) == 0:
            continue

        seg_path = (audit / s["segment_file_v2"]).resolve()
        if not seg_path.is_relative_to(audit):
            raise ValueError(f"Ruta de segmento fuera de auditoria: {s['segment_id']}")
        if sha(seg_path) != s["segment_sha256_v2"]:
            raise ValueError(f"Segmento modificado: {s['segment_id']}")

        rows = list(readcsv(seg_path))
        mid, sid = s["monitoring_location_id"], s["segment_id"]
        first = date.fromisoformat(s["date_min"])
        last = date.fromisoformat(s["date_max"])
        gmax = int(s["max_gap_missing_days"])

        for L in relevant_lengths(gmax):
            cand = enumerate_candidates_reserve(
                rows, mid, first, last, L, int(c["guard_days"])
            )
            maximal = max_nonoverlap(cand)
            panel = balanced_ranked_panel(
                maximal, int(c["max_masks_per_length"]), int(c["seed"]),
                mid, sid, L
            )

            for rank, x in enumerate(panel, 1):
                mask_id = hashlib.sha256(
                    f"{mid}|{sid}|reserve|{L}|{x['start']}|{c['seed']}".encode()
                ).hexdigest()[:20]
                reserve_windows.append(dict(
                    monitoring_location_id=mid,
                    site_no=s["site_no"],
                    estado=s["estado"],
                    segment_id=sid,
                    gap_length_days=L,
                    mask_id=mask_id,
                    start_date=x["start"].isoformat(),
                    end_date=x["end"].isoformat(),
                    guard_left_start=x["left0"].isoformat(),
                    guard_left_end=x["left1"].isoformat(),
                    guard_right_start=x["right0"].isoformat(),
                    guard_right_end=x["right1"].isoformat(),
                    year=x["year"],
                    quarter=x["quarter"],
                    segment_zone=x["zone"],
                    phase="reservada",
                    selection_rank=rank,
                ))

            years = {x["year"] for x in panel}
            quarters = {x["quarter"] for x in panel}
            zones = {x["zone"] for x in panel}

            note = "ok"
            if len(panel) < 10:
                note = "menos_de_10_ventanas_seleccionables"
            elif len(quarters) < 2:
                note = "cobertura_trimestral_limitada"
            elif len(zones) < 2:
                note = "cobertura_segmento_limitada"

            reserve_audit.append(dict(
                monitoring_location_id=mid,
                site_no=s["site_no"],
                estado=s["estado"],
                segment_id=sid,
                duration_years_approx=s["duration_years_approx"],
                max_gap_missing_days=gmax,
                gap_length_days=L,
                n_candidate_windows_raw=len(cand),
                n_nonoverlap_capacity=len(maximal),
                n_selected_panel=len(panel),
                n_years_selected=len(years),
                n_quarters_selected=len(quarters),
                n_zones_selected=len(zones),
                can_10=int(len(panel) >= 10),
                can_20=int(len(panel) >= 20),
                can_30=int(len(panel) >= 30),
                can_50=int(len(panel) >= 50),
                coverage_note=note,
            ))

        if i % 25 == 0 or i == len(selected):
            print(
                f"Auditoria pre-reserva: {i}/{len(selected)} segmentos seleccionados",
                flush=True
            )

    reserve_by_sid = defaultdict(list)
    for r in reserve_audit:
        reserve_by_sid[r["segment_id"]].append(r)

    interpolation_selected = [
        s for s in selected if int(s["missing_days"]) > 0
    ]
    if set(reserve_by_sid) != {s["segment_id"] for s in interpolation_selected}:
        raise ValueError("La auditoria de reserva no cubre todos los seleccionados con huecos")

    reserve_summary = []
    lengths_present = sorted(
        {int(r["gap_length_days"]) for r in reserve_audit}
    )

    for L in lengths_present:
        rs = [r for r in reserve_audit if int(r["gap_length_days"]) == L]
        for B in BUDGETS:
            n = sum(int(r[f"can_{B}"]) for r in rs)
            reserve_summary.append(dict(
                gap_length_days=L,
                budget_masks=B,
                n_segment_lengths=len(rs),
                n_support=n,
                pct_support=100 * n / len(rs) if rs else "",
            ))

    for B in BUDGETS:
        n_all = sum(
            all(int(r[f"can_{B}"]) for r in rs)
            for rs in reserve_by_sid.values()
        )
        reserve_summary.append(dict(
            gap_length_days="ALL_REQUIRED",
            budget_masks=B,
            n_segment_lengths=len(interpolation_selected),
            n_support=n_all,
            pct_support=(
                100 * n_all / len(interpolation_selected)
                if interpolation_selected else ""
            ),
        ))

    spec = dict(
        version=VERSION,
        config=c,
        inputs={
            "audit_summary": sha(audit / "resumen.json"),
            "development_summary": sha(dev / "resumen.json"),
            "segments": sha(audit / "segmentos_elegibles_30d.csv"),
            "development_status": sha(dev / "estado_segmentos.csv"),
            "development_metrics": sha(dev / "metricas_segmento_longitud.csv"),
            "estado_por_pozo_gap30": sha(ex / "02_segmentacion" / "estado_por_pozo.csv"),
        },
        script_sha256=sha(Path(__file__)),
        python=platform.python_version(),
        selection_rule=(
            "development_usable -> longest duration -> greater completeness -> "
            "earliest start -> segment_id"
        ),
        development_mask_budget=50,
        reserve_values_evaluated=False,
    )
    token = hashlib.sha256(
        json.dumps(spec, sort_keys=True).encode()
    ).hexdigest()[:12]
    root = ex / "07_seleccion_pre_reserva_v2" / ("seleccion_" + token)
    root.mkdir(parents=True, exist_ok=True)

    writecsv(
        root / "decisiones_segmentos_por_pozo.csv",
        decisions, DECISION_FIELDS
    )
    writecsv(
        root / "segmentos_seleccionados_pre_reserva.csv",
        selected, SELECTED_FIELDS
    )
    writecsv(
        root / "segmentos_recuperados_seleccionados.csv",
        recovered_selected, RECOVERED_FIELDS
    )
    writecsv(
        root / "pozos_sin_segmento_pass.csv",
        no_pass_wells,
        ["monitoring_location_id", "n_candidates", "reason"]
    )
    writecsv(
        root / "resumen_recuperacion_segmentacion.csv",
        recovery_rows,
        [
            "grupo", "n_wells", "n_source_daily_values_retained",
            "n_eligible_truth_values", "n_calendar_days_final_grid",
            "n_days_to_interpolate_later", "observed_equivalent_years",
            "sum_segment_duration_years", "median_segment_duration_years",
            "min_segment_duration_years", "max_segment_duration_years",
        ]
    )
    writecsv(
        root / "auditoria_ventanas_reserva.csv",
        reserve_audit, RESERVE_AUDIT_FIELDS
    )
    writecsv(
        root / "manifiesto_ventanas_reserva_max.csv",
        reserve_windows, RESERVE_WINDOW_FIELDS
    )
    writecsv(
        root / "resumen_disponibilidad_reserva.csv",
        reserve_summary,
        [
            "gap_length_days", "budget_masks", "n_segment_lengths",
            "n_support", "pct_support"
        ]
    )

    fallback_ranks = Counter(
        int(r["structural_rank_all"])
        for r in selected if int(r["fallback_used"])
    )
    reserve_all = {
        int(r["budget_masks"]): {
            "n_support": int(r["n_support"]),
            "n_total": int(r["n_segment_lengths"]),
            "pct_support": optional_float(r["pct_support"]),
        }
        for r in reserve_summary
        if r["gap_length_days"] == "ALL_REQUIRED"
    }

    summary_core = dict(
        etapa_completa=True,
        reserved_values_evaluated=False,
        real_gaps_interpolated=False,
        n_wells_temporally_eligible=len(by_well),
        n_segments_temporally_eligible=len(segs),
        n_segments_development_usable=n_dev_usable,
        n_segments_development_fail=len(segs) - n_dev_usable,
        development_statuses=dict(sorted(counts_eval.items())),
        n_wells_selected_pre_reserve=len(selected),
        n_wells_without_development_pass=len(no_pass_wells),
        n_selected_without_interpolation=sum(
            int(r["missing_days"]) == 0 for r in selected
        ),
        n_selected_requiring_interpolation=len(interpolation_selected),
        n_wells_using_fallback=sum(int(r["fallback_used"]) for r in selected),
        fallback_structural_ranks=dict(sorted(fallback_ranks.items())),
        n_direct_selected=len(direct_selected),
        n_recovered_by_segmentation_selected=len(recovered_selected),
        recovered_source_daily_values_retained=sum(
            int(r["n_days"]) for r in recovered_selected
        ),
        recovered_expected_calendar_days=sum(
            int(r["expected_days"]) for r in recovered_selected
        ),
        recovered_days_to_interpolate_later=sum(
            int(r["missing_days"]) for r in recovered_selected
        ),
        recovered_observed_equivalent_years=sum(
            int(r["n_days"]) for r in recovered_selected
        ) / 365.25 if recovered_selected else 0,
        recovered_sum_segment_duration_years=sum(
            float(r["duration_years_approx"]) for r in recovered_selected
        ),
        reserve_support_all_required=reserve_all,
    )

    outputs = [
        root / "decisiones_segmentos_por_pozo.csv",
        root / "segmentos_seleccionados_pre_reserva.csv",
        root / "segmentos_recuperados_seleccionados.csv",
        root / "pozos_sin_segmento_pass.csv",
        root / "resumen_recuperacion_segmentacion.csv",
        root / "auditoria_ventanas_reserva.csv",
        root / "manifiesto_ventanas_reserva_max.csv",
        root / "resumen_disponibilidad_reserva.csv",
    ]

    summary = dict(
        **summary_core,
        finalizado_utc=now(),
        **spec,
        output_hashes={p.name: sha(p) for p in outputs},
        note=(
            "Seleccion pre-reserva congelada. La reserva fue auditada solo por "
            "fechas/banderas; no se calcularon errores reservados."
        ),
    )
    writejson(root / "resumen_pre_reserva.json", summary)

    pointer = ex / "07_seleccion_pre_reserva_v2" / "ultima_seleccion.txt"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    tmp = pointer.with_suffix(".tmp")
    tmp.write_text(str(root) + "\n", encoding="utf-8")
    tmp.replace(pointer)

    print("\nSELECCION PRE-RESERVA COMPLETA; VALORES RESERVADOS NO EVALUADOS")
    print(json.dumps(summary_core, indent=2, ensure_ascii=False))
    print("Resultados:", root)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrumpido; no se evaluaron valores reservados.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
