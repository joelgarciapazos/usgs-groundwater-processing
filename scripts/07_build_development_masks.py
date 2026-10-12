#!/usr/bin/env python3
"""Build and audit development masking windows for all eligible 30-day-scenario segments without opening the reserved years."""
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

from continuity_common import (
    DEFAULT_QA, boolean, experiment, metrics, now, prepared, readcsv, readjson,
    require_stage, segment, sha, writecsv, writejson,
)

VERSION = "2.0.1"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "validation.json"
BASE_LENGTHS = (1, 3, 7, 15, 30)
BUDGETS = (10, 20, 30, 50)

SEGMENT_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "gap_scenario", "segment_id",
    "date_min", "date_max", "elapsed_days", "duration_years_approx", "n_days",
    "expected_days", "missing_days", "completeness", "max_gap_missing_days",
    "eligible_duration", "truth_days", "estimated_days", "jump_days",
    "selected_old", "segment_file_v2", "segment_sha256_v2",
    "fraction_to_interpolate", "n_eligible_segments_well_30d",
]

WINDOW_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id", "gap_scenario",
    "gap_length_days", "mask_id", "start_date", "end_date", "guard_left_start",
    "guard_left_end", "guard_right_start", "guard_right_end", "year", "quarter",
    "segment_zone", "phase", "selection_rank",
]

AUDIT_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id", "gap_scenario",
    "duration_years_approx", "completeness", "missing_days",
    "fraction_to_interpolate", "max_gap_missing_days", "gap_length_days",
    "n_candidate_windows_raw", "n_nonoverlap_capacity", "n_selected_panel",
    "n_years_selected", "n_quarters_selected", "n_zones_selected",
    "can_10", "can_20", "can_30", "can_50", "coverage_note",
]


def phase(mid: str, year: int) -> str:
    """Misma regla determinista ya usada en multiescala_common.py."""
    offset = int(hashlib.sha256(mid.encode()).hexdigest()[:8], 16) % 5
    return "reservada" if year % 5 == offset else "desarrollo"


def stable_key(seed: int, *parts: object) -> str:
    return hashlib.sha256("|".join(map(str, (seed,) + parts)).encode()).hexdigest()


def configuration(path: Path) -> dict:
    c = readjson(path)
    required = {
        "gap_scenario_days": int,
        "min_years": int,
        "guard_days": int,
        "seed": int,
        "max_masks_per_length": int,
        "max_interpolated_fraction": (int, float),
    }
    for key, typ in required.items():
        if key not in c or not isinstance(c[key], typ) or isinstance(c[key], bool):
            raise ValueError(f"Configuracion invalida: {key}")
    if c["gap_scenario_days"] != 30:
        raise ValueError("Esta version esta cerrada para gap_scenario_days=30")
    if c["min_years"] != 10:
        raise ValueError("Esta version esta cerrada para min_years=10")
    if c["guard_days"] != 3:
        raise ValueError("guard_days debe permanecer exactamente en 3")
    if c["seed"] != 42:
        raise ValueError("La semilla congelada debe ser 42")
    if c["max_masks_per_length"] != 50:
        raise ValueError("max_masks_per_length debe permanecer exactamente en 50")
    if not math.isclose(float(c["max_interpolated_fraction"]), 0.10):
        raise ValueError("max_interpolated_fraction debe permanecer en 0.10")
    if list(c.get("budget_sensitivity", [])) != [10, 20, 30, 50]:
        raise ValueError("budget_sensitivity debe permanecer en [10, 20, 30, 50]")
    if int(c.get("budget_resamples", 0)) != 100:
        raise ValueError("budget_resamples debe permanecer en 100")
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
    p.add_argument("--experimento", type=Path)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return p


def relevant_lengths(gmax: int) -> list[int]:
    if gmax <= 0:
        return []
    if gmax > 30:
        raise ValueError(f"Gmax fuera del escenario 30 d: {gmax}")
    return sorted(set([L for L in BASE_LENGTHS if L <= gmax] + [gmax]))


def zone_for(d: date, first: date, last: date) -> str:
    span = max(1, (last - first).days)
    f = (d - first).days / span
    return "inicio" if f < 1/3 else "centro" if f < 2/3 else "final"


def enumerate_candidates(rows: list[dict], mid: str, first: date, last: date,
                         length: int, guard: int) -> list[dict]:
    """Usa solo fechas/banderas. No consulta depth_to_water_m para seleccionar ventanas."""
    by_date = {date.fromisoformat(r["date"]): r for r in rows}
    candidates = []
    start = first + timedelta(days=guard)
    latest = last - timedelta(days=length + guard - 1)
    while start <= latest:
        end = start + timedelta(days=length - 1)
        left0, left1 = start - timedelta(days=guard), start - timedelta(days=1)
        right0, right1 = end + timedelta(days=1), end + timedelta(days=guard)
        full = [left0 + timedelta(days=i) for i in range((right1-left0).days + 1)]
        # Mantener cada prueba dentro de un ano calendario evita cruzar limites de fase.
        same_year = all(d.year == start.year for d in full)
        if same_year and phase(mid, start.year) == "desarrollo":
            recs = [by_date.get(d) for d in full]
            if all(r is not None and boolean(r["eligible_truth"]) for r in recs):
                candidates.append(dict(
                    start=start, end=end, left0=left0, left1=left1,
                    right0=right0, right1=right1, year=start.year,
                    quarter=(start.month-1)//3 + 1,
                    zone=zone_for(start, first, last),
                ))
        start += timedelta(days=1)
    return candidates


def max_nonoverlap(candidates: list[dict]) -> list[dict]:
    """Interval scheduling exacto para intervalos de igual longitud: termina primero."""
    chosen = []
    last_end = None
    for c in sorted(candidates, key=lambda x: (x["end"], x["start"])):
        if last_end is None or c["start"] > last_end:
            chosen.append(c)
            last_end = c["end"]
    return chosen


def balanced_panel(maximal: list[dict], maximum: int, seed: int,
                   mid: str, sid: str, length: int) -> list[dict]:
    """Submuestrea el conjunto no solapado maximo equilibrando celdas ano/Q/zona."""
    if len(maximal) <= maximum:
        return sorted(maximal, key=lambda x: x["start"])
    groups = defaultdict(list)
    for c in maximal:
        groups[(c["year"], c["quarter"], c["zone"])].append(c)
    for key, vals in groups.items():
        vals.sort(key=lambda x: stable_key(seed, mid, sid, length, key, x["start"].isoformat()))
    keys = sorted(groups, key=lambda k: stable_key(seed, mid, sid, length, "group", *k))
    out = []
    i = 0
    while len(out) < maximum:
        progress = False
        for key in keys:
            if i < len(groups[key]):
                out.append(groups[key][i]); progress = True
                if len(out) == maximum:
                    break
        if not progress:
            break
        i += 1
    return sorted(out, key=lambda x: x["start"])


def compare_segment(expected: dict, actual: dict) -> None:
    exact = ["date_min", "date_max", "elapsed_days", "n_days", "expected_days",
             "missing_days", "max_gap_missing_days", "eligible_duration"]
    for key in exact:
        if str(actual[key]) != str(expected[key]):
            raise ValueError(f"Segmento {expected['segment_id']}: {key} no concilia ({actual[key]} != {expected[key]})")
    if not math.isclose(float(actual["completeness"]), float(expected["completeness"]), rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError(f"Segmento {expected['segment_id']}: completeness no concilia")


def main() -> None:
    args = parser().parse_args()
    c = configuration(args.config.resolve())
    qa = args.carpeta.expanduser().resolve()

    # Reusa la validacion de hashes del experimento sin modificar scripts historicos.
    class A: pass
    a = A(); a.carpeta = qa; a.experimento = args.experimento
    ex, old_config, manifest = experiment(a)
    require_stage(ex, "01_preparados")
    seg_report = require_stage(ex, "02_segmentacion")

    all_path = ex / "02_segmentacion" / "segmentos_todos.csv"
    state_path = ex / "02_segmentacion" / "estado_por_pozo.csv"
    prep_path = ex / "01_preparados" / "inventario_preparado.csv"
    eligible = [r for r in readcsv(all_path)
                if int(r["gap_scenario"]) == c["gap_scenario_days"] and boolean(r["eligible_duration"])]
    if not eligible:
        raise ValueError("No se encontraron segmentos elegibles en gap=30")

    spec = dict(
        version=VERSION,
        config=c,
        inputs={
            "segmentos_todos.csv": sha(all_path),
            "estado_por_pozo.csv": sha(state_path),
            "inventario_preparado.csv": sha(prep_path),
            "02_segmentacion_resumen.json": sha(ex/"02_segmentacion"/"resumen.json"),
        },
        script_sha256=sha(Path(__file__)),
        python=platform.python_version(),
        protocol_note="Reserva no evaluada; seleccion de ventanas usa solo fechas y banderas.",
    )
    token = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    root = ex / "05_validacion_segmentos_v2" / ("auditoria_" + token)
    root.mkdir(parents=True, exist_ok=True)
    writejson(root/"resumen.json", dict(etapa_completa=False, inicio_utc=now(), **spec))

    expected_by_well = defaultdict(dict)
    for r in eligible:
        key = r["segment_id"]
        if key in expected_by_well[r["monitoring_location_id"]]:
            raise ValueError(f"Segmento duplicado en gap30: {key}")
        expected_by_well[r["monitoring_location_id"]][key] = r

    nelig_by_well = Counter(r["monitoring_location_id"] for r in eligible)
    prepared_inventory = list(readcsv(prep_path))
    prep_by_well = {r["monitoring_location_id"]: r for r in prepared_inventory}

    exported = []
    audits = []
    windows = []
    errors = []

    for wi, (mid, expected_map) in enumerate(sorted(expected_by_well.items()), 1):
        try:
            w = prep_by_well[mid]
            rows = prepared(ex, w)
            parts = segment(rows, c["gap_scenario_days"])
            actual_map = {}
            for part in parts:
                met = metrics(part, c["min_years"])
                sid = hashlib.sha256((mid + "|" + part[0]["date"] + "|" + part[-1]["date"] + "|" + w["prepared_sha256"]).encode()).hexdigest()[:20]
                if met["eligible_duration"]:
                    actual_map[sid] = (part, met)
            if set(actual_map) != set(expected_map):
                raise ValueError(f"{mid}: segmentos elegibles reconstruidos no coinciden con inventario 08")

            for sid, expected in sorted(expected_map.items()):
                part, met = actual_map[sid]
                compare_segment(expected, met)
                truth_days = sum(boolean(r["eligible_truth"]) for r in part)
                estimated_days = sum(boolean(r["source_estimated_effective"]) for r in part)
                jump_days = sum(boolean(r["jump_alert"]) for r in part)
                if truth_days != int(expected["truth_days"]):
                    raise ValueError(f"{sid}: truth_days no concilia")

                segfile = root/"segmentos_elegibles"/expected["estado"]/(f"{mid}_{sid}.csv.gz")
                writecsv(segfile, part, list(part[0]))
                segsha = sha(segfile)
                frac = int(met["missing_days"]) / int(met["expected_days"])
                outrow = dict(
                    monitoring_location_id=mid, site_no=expected["site_no"], estado=expected["estado"],
                    gap_scenario=c["gap_scenario_days"], segment_id=sid,
                    **met, truth_days=truth_days, estimated_days=estimated_days,
                    jump_days=jump_days, selected_old=expected["selected"],
                    segment_file_v2=str(segfile.relative_to(root)), segment_sha256_v2=segsha,
                    fraction_to_interpolate=frac,
                    n_eligible_segments_well_30d=nelig_by_well[mid],
                )
                exported.append(outrow)

                first, last = date.fromisoformat(met["date_min"]), date.fromisoformat(met["date_max"])
                gmax = int(met["max_gap_missing_days"])
                for L in relevant_lengths(gmax):
                    cand = enumerate_candidates(part, mid, first, last, L, c["guard_days"])
                    maximal = max_nonoverlap(cand)
                    panel = balanced_panel(maximal, c["max_masks_per_length"], c["seed"], mid, sid, L)
                    for rank, x in enumerate(panel, 1):
                        mask_id = hashlib.sha256((f"{mid}|{sid}|{L}|{x['start']}|{c['seed']}").encode()).hexdigest()[:20]
                        windows.append(dict(
                            monitoring_location_id=mid, site_no=expected["site_no"], estado=expected["estado"],
                            segment_id=sid, gap_scenario=30, gap_length_days=L, mask_id=mask_id,
                            start_date=x["start"].isoformat(), end_date=x["end"].isoformat(),
                            guard_left_start=x["left0"].isoformat(), guard_left_end=x["left1"].isoformat(),
                            guard_right_start=x["right0"].isoformat(), guard_right_end=x["right1"].isoformat(),
                            year=x["year"], quarter=x["quarter"], segment_zone=x["zone"],
                            phase="desarrollo", selection_rank=rank,
                        ))
                    years = {x["year"] for x in panel}; qs = {x["quarter"] for x in panel}; zs = {x["zone"] for x in panel}
                    note = "ok"
                    if len(panel) < 10: note = "menos_de_10_ventanas_seleccionables"
                    elif len(qs) < 2: note = "cobertura_trimestral_limitada"
                    elif len(zs) < 2: note = "cobertura_segmento_limitada"
                    audits.append(dict(
                        monitoring_location_id=mid, site_no=expected["site_no"], estado=expected["estado"],
                        segment_id=sid, gap_scenario=30,
                        duration_years_approx=met["duration_years_approx"], completeness=met["completeness"],
                        missing_days=met["missing_days"], fraction_to_interpolate=frac,
                        max_gap_missing_days=gmax, gap_length_days=L,
                        n_candidate_windows_raw=len(cand), n_nonoverlap_capacity=len(maximal),
                        n_selected_panel=len(panel), n_years_selected=len(years),
                        n_quarters_selected=len(qs), n_zones_selected=len(zs),
                        can_10=int(len(panel)>=10), can_20=int(len(panel)>=20),
                        can_30=int(len(panel)>=30), can_50=int(len(panel)>=50),
                        coverage_note=note,
                    ))
        except (ValueError, OSError, KeyError) as exc:
            errors.append(dict(monitoring_location_id=mid, error=str(exc)))
        if wi % 20 == 0 or wi == len(expected_by_well):
            print(f"Auditoria ventanas: {wi}/{len(expected_by_well)} pozos; errores {len(errors)}", flush=True)

    if errors:
        writecsv(root/"errores.csv", errors, ["monitoring_location_id", "error"])
        writejson(root/"resumen.json", dict(etapa_completa=False, errores=len(errors), **spec))
        raise ValueError(f"Hay errores tecnicos; revisar {root/'errores.csv'}")

    # Conciliaciones estructurales
    if len(exported) != len(eligible):
        raise ValueError(f"Segmentos exportados {len(exported)} != elegibles {len(eligible)}")
    wells_eligible = {r["monitoring_location_id"] for r in exported}

    # Reconcile the gap-30 flow against the prepared inventory and segmentation outputs.
    states30 = [r for r in readcsv(state_path) if int(r["gap_scenario"]) == 30]
    state_by = {r["monitoring_location_id"]: r for r in states30}
    span10 = {r["monitoring_location_id"] for r in prepared_inventory if boolean(r["eligible_duration"])}
    direct = {mid for mid, r in state_by.items() if r["origin"] == "directo"}
    recovered = {mid for mid, r in state_by.items() if r["origin"] == "recuperado_segmentacion"}
    requiring = span10 - direct
    unrecovered_span10 = requiring - recovered
    flow = dict(
        n_u_candidates=len(prepared_inventory),
        n_span_ge_10_exact_anniversary=len(span10),
        n_span_lt_10=len(prepared_inventory)-len(span10),
        n_direct_gap30=len(direct),
        n_span_ge_10_requiring_segmentation=len(requiring),
        n_recovered_gap30=len(requiring & recovered),
        n_span_ge_10_not_recovered=len(unrecovered_span10),
        n_temporally_eligible_wells_gap30=len(wells_eligible),
        n_eligible_segments_gap30=len(exported),
        n_wells_multiple_eligible_segments=sum(v>1 for v in nelig_by_well.values()),
        distribution_n_eligible_segments_per_well=dict(sorted(Counter(nelig_by_well.values()).items())),
    )

    # Resumen por longitud/presupuesto.
    budget_rows = []
    for L in sorted({int(r["gap_length_days"]) for r in audits}):
        rs = [r for r in audits if int(r["gap_length_days"]) == L]
        for B in BUDGETS:
            budget_rows.append(dict(
                gap_length_days=L, budget_masks=B, n_segment_lengths=len(rs),
                n_support=sum(int(r[f"can_{B}"]) for r in rs),
                pct_support=100*sum(int(r[f"can_{B}"]) for r in rs)/len(rs) if rs else "",
            ))
    # Resumen nacional considerando que un segmento debe cubrir TODAS sus longitudes requeridas.
    by_sid = defaultdict(list)
    for r in audits: by_sid[r["segment_id"]].append(r)
    for B in BUDGETS:
        eligible_gap_segments = [sid for sid, rs in by_sid.items() if rs]
        n_all = sum(all(int(r[f"can_{B}"]) for r in rs) for sid, rs in by_sid.items())
        budget_rows.append(dict(
            gap_length_days="ALL_REQUIRED", budget_masks=B,
            n_segment_lengths=len(eligible_gap_segments), n_support=n_all,
            pct_support=100*n_all/len(eligible_gap_segments) if eligible_gap_segments else "",
        ))

    writecsv(root/"segmentos_elegibles_30d.csv", exported, SEGMENT_FIELDS)
    writecsv(root/"manifiesto_ventanas_desarrollo.csv", windows, WINDOW_FIELDS)
    writecsv(root/"auditoria_ventanas.csv", audits, AUDIT_FIELDS)
    writecsv(root/"resumen_presupuestos.csv", budget_rows,
             ["gap_length_days", "budget_masks", "n_segment_lengths", "n_support", "pct_support"])
    writecsv(root/"errores.csv", [], ["monitoring_location_id", "error"])
    writejson(root/"auditoria_flujo.json", flow)

    outputs = [
        root/"segmentos_elegibles_30d.csv", root/"manifiesto_ventanas_desarrollo.csv",
        root/"auditoria_ventanas.csv", root/"resumen_presupuestos.csv",
        root/"errores.csv", root/"auditoria_flujo.json",
    ]
    summary = dict(
        etapa_completa=True, finalizado_utc=now(), **spec,
        auditoria_flujo=flow,
        n_windows_selected=len(windows),
        output_hashes={p.name: sha(p) for p in outputs},
        segment_files_hash_manifest={r["segment_id"]: r["segment_sha256_v2"] for r in exported},
        reserved_evaluated=False,
        note="Paneles de desarrollo congelados antes de calcular errores. Reserva intacta.",
    )
    writejson(root/"resumen.json", summary)
    pointer = ex/"05_validacion_segmentos_v2"/"ultima_auditoria.txt"
    tmp = pointer.with_suffix(".tmp"); tmp.write_text(str(root)+"\n", encoding="utf-8"); tmp.replace(pointer)

    print("\nAUDITORIA COMPLETA")
    print(json.dumps(flow, indent=2, ensure_ascii=False))
    print("Resultados:", root)
    print("Traer primero: auditoria_flujo.json, resumen_presupuestos.csv y auditoria_ventanas.csv")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrumpido; no se alteraron etapas previas.", file=sys.stderr); sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr); sys.exit(2)
