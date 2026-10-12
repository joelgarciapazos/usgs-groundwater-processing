#!/usr/bin/env python3
"""Open the pre-frozen temporal reserve once and perform final independent PCHIP validation.

One segment per well is fixed before reserve evaluation. Twenty reserved masks are evaluated for every relevant mask length, PCHIP is used without extrapolation, RMSE must be <=0.20 m at every required length, and no alternate segment is tried after reserve failure. Natural gaps are not interpolated in this stage."""
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
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import scipy
from scipy.interpolate import PchipInterpolator

from continuity_common import DEFAULT_QA, boolean, now, readcsv, readjson, sha, writecsv, writejson

VERSION = "1.0.0"
RESERVE_BUDGET = 20
BASE_LENGTHS = (1, 3, 7, 15, 30)

MASK_FIELDS = [
    "monitoring_location_id", "segment_id", "gap_length_days", "mask_id",
    "selection_rank", "start_date", "end_date", "n_test",
    "sse_m2", "sae_m", "sum_error_m", "rmse_m", "mae_m",
    "bias_m", "p95_abs_error_m",
]

DAY_FIELDS = [
    "monitoring_location_id", "segment_id", "gap_length_days", "mask_id",
    "date", "observed_m", "predicted_m", "error_m", "abs_error_m",
]

LENGTH_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id",
    "gap_length_days", "n_masks", "n_days_evaluated",
    "rmse_m", "mae_m", "bias_m", "p95_abs_error_m",
    "rmse_threshold_m", "pass_rmse",
]

FINAL_FIELDS = [
    "monitoring_location_id", "site_no", "estado", "segment_id",
    "origin_gap30", "duration_years_approx", "date_min", "date_max",
    "completeness", "n_days", "expected_days", "missing_days",
    "fraction_to_interpolate", "max_gap_missing_days",
    "development_status", "reserve_status", "final_status",
    "n_required_lengths", "max_reserved_rmse_m",
    "fallback_after_reserve",
]


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--carpeta", type=Path, default=DEFAULT_QA)
    p.add_argument("--experimento", type=Path)
    p.add_argument("--seleccion", type=Path)
    return p


def phase(mid: str, year: int) -> str:
    offset = int(hashlib.sha256(mid.encode()).hexdigest()[:8], 16) % 5
    return "reservada" if year % 5 == offset else "desarrollo"


def relevant_lengths(gmax: int) -> list[int]:
    if gmax <= 0:
        return []
    if gmax > 30:
        raise ValueError(f"Gmax fuera del escenario de 30 dias: {gmax}")
    return sorted(set([x for x in BASE_LENGTHS if x <= gmax] + [gmax]))


def locate(args):
    qa = args.carpeta.expanduser().resolve()
    ex = args.experimento.expanduser().resolve() if args.experimento else Path(
        (qa/"07_continuidad_exploratoria"/"ultimo_experimento.txt").read_text().strip()
    ).resolve()

    ex_manifest = readjson(ex/"manifiesto.json")
    if ex_manifest["carpeta_qa"] != str(qa):
        raise ValueError("El experimento no pertenece a la carpeta QA indicada")

    sel = args.seleccion.expanduser().resolve() if args.seleccion else Path(
        (ex/"07_seleccion_pre_reserva_v2"/"ultima_seleccion.txt").read_text().strip()
    ).resolve()
    if not sel.is_relative_to(ex):
        raise ValueError("La seleccion pre-reserva no pertenece al experimento")

    summary_path = sel/"resumen_pre_reserva.json"
    summary = readjson(summary_path)

    if not summary.get("etapa_completa"):
        raise ValueError("Seleccion pre-reserva incompleta")
    if summary.get("reserved_values_evaluated") is not False:
        raise ValueError("La reserva ya aparece evaluada en la seleccion")
    if summary.get("real_gaps_interpolated") is not False:
        raise ValueError("Los huecos reales ya aparecen interpolados")
    n_selected_summary = int(summary.get("n_wells_selected_pre_reserve", -1))
    n_interp_summary = int(summary.get("n_selected_requiring_interpolation", -1))
    n_nointerp_summary = int(summary.get("n_selected_without_interpolation", -1))
    if min(n_selected_summary, n_interp_summary, n_nointerp_summary) < 0:
        raise ValueError("Pre-reserve summary is missing required selection counts")
    if n_interp_summary + n_nointerp_summary != n_selected_summary:
        raise ValueError("Pre-reserve interpolation counts do not reconcile")

    # Verificar todos los outputs congelados.
    for name, digest in summary.get("output_hashes", {}).items():
        p = sel/name
        if not p.exists() or sha(p) != digest:
            raise ValueError(f"Archivo pre-reserva modificado: {name}")

    # Localizar la auditoria original usando el puntero, y confirmar el hash
    # registrado en la seleccion pre-reserva.
    audit = Path(
        (ex/"05_validacion_segmentos_v2"/"ultima_auditoria.txt").read_text().strip()
    ).resolve()
    if not audit.is_relative_to(ex):
        raise ValueError("Auditoria fuera del experimento")
    if sha(audit/"resumen.json") != summary["inputs"]["audit_summary"]:
        raise ValueError("La auditoria actual no coincide con la usada para seleccionar")

    audit_summary = readjson(audit/"resumen.json")
    if not audit_summary.get("etapa_completa") or audit_summary.get("reserved_evaluated") is not False:
        raise ValueError("Auditoria original incompleta o alterada")

    return qa, ex, audit, sel, summary


def read_segment(audit: Path, s: dict) -> dict[date, dict]:
    p = (audit/s["segment_file_v2"]).resolve()
    if not p.is_relative_to(audit):
        raise ValueError(f"Ruta de segmento fuera de auditoria: {s['segment_id']}")
    if sha(p) != s["segment_sha256_v2"]:
        raise ValueError(f"Hash de segmento modificado: {s['segment_id']}")

    out = {}
    for r in readcsv(p):
        d = date.fromisoformat(r["date"])
        if d in out:
            raise ValueError(f"Fecha duplicada en {s['segment_id']}: {d}")
        out[d] = r
    return out


def evaluate_mask(mid: str, sid: str, L: int, m: dict,
                  by_date: dict[date, dict], guard: int):
    start = date.fromisoformat(m["start_date"])
    end = date.fromisoformat(m["end_date"])

    hidden = [start + timedelta(days=i) for i in range(L)]
    left = [start - timedelta(days=i) for i in range(guard, 0, -1)]
    right = [end + timedelta(days=i) for i in range(1, guard+1)]
    all_dates = left + hidden + right

    if len(hidden) != L or hidden[-1] != end:
        raise ValueError(f"Mascara {m['mask_id']}: longitud inconsistente")
    if any(d not in by_date for d in all_dates):
        raise ValueError(f"Mascara {m['mask_id']}: contexto no disponible")
    if any(d.year != start.year for d in all_dates):
        raise ValueError(f"Mascara {m['mask_id']}: cruza ano calendario")
    if any(phase(mid, d.year) != "reservada" for d in all_dates):
        raise ValueError(f"Mascara {m['mask_id']}: no pertenece totalmente a reserva")
    if any(not boolean(by_date[d]["eligible_truth"]) for d in all_dates):
        raise ValueError(f"Mascara {m['mask_id']}: truth/contexto no elegible")

    # PCHIP usa exclusivamente los 3 vecinos elegibles a cada lado, igual que
    # el protocolo de desarrollo. Los dias ocultados no entran al ajuste.
    origin = start.toordinal()
    train_dates = left + right
    x_train = np.asarray([d.toordinal()-origin for d in train_dates], dtype=float)
    y_train = np.asarray([float(by_date[d]["depth_to_water_m"]) for d in train_dates], dtype=float)
    x_test = np.asarray([d.toordinal()-origin for d in hidden], dtype=float)
    truth = np.asarray([float(by_date[d]["depth_to_water_m"]) for d in hidden], dtype=float)

    pred = np.asarray(
        PchipInterpolator(x_train, y_train, extrapolate=False)(x_test),
        dtype=float
    )
    if not np.isfinite(pred).all() or not np.isfinite(truth).all():
        raise ValueError(f"Mascara {m['mask_id']}: prediccion/verdad no finita")

    e = pred - truth
    summary = dict(
        monitoring_location_id=mid,
        segment_id=sid,
        gap_length_days=L,
        mask_id=m["mask_id"],
        selection_rank=int(m["selection_rank"]),
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        n_test=len(e),
        sse_m2=float(np.sum(e*e)),
        sae_m=float(np.sum(np.abs(e))),
        sum_error_m=float(np.sum(e)),
        rmse_m=float(np.sqrt(np.mean(e*e))),
        mae_m=float(np.mean(np.abs(e))),
        bias_m=float(np.mean(e)),
        p95_abs_error_m=float(np.quantile(np.abs(e), 0.95)),
    )

    days = [
        dict(
            monitoring_location_id=mid,
            segment_id=sid,
            gap_length_days=L,
            mask_id=m["mask_id"],
            date=d.isoformat(),
            observed_m=float(t),
            predicted_m=float(p),
            error_m=float(er),
            abs_error_m=float(abs(er)),
        )
        for d, t, p, er in zip(hidden, truth, pred, e)
    ]
    return summary, days


def write_gz(path: Path, rows: list[dict], fields: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+".tmp")
    with tmp.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with io.TextIOWrapper(gz, encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
    tmp.replace(path)


def aggregate_length(mask_rows: list[dict], day_rows: list[dict],
                     threshold: float):
    n = sum(int(r["n_test"]) for r in mask_rows)
    if not n:
        raise ValueError("No hay dias evaluados en una longitud reservada")
    rmse = math.sqrt(sum(float(r["sse_m2"]) for r in mask_rows)/n)
    mae = sum(float(r["sae_m"]) for r in mask_rows)/n
    bias = sum(float(r["sum_error_m"]) for r in mask_rows)/n
    p95 = float(np.quantile(
        np.asarray([float(r["abs_error_m"]) for r in day_rows], dtype=float),
        0.95
    ))
    return dict(
        n_masks=len(mask_rows),
        n_days_evaluated=n,
        rmse_m=rmse,
        mae_m=mae,
        bias_m=bias,
        p95_abs_error_m=p95,
        rmse_threshold_m=threshold,
        pass_rmse=int(rmse <= threshold),
    )


def main():
    args = parser().parse_args()
    qa, ex, audit, sel, pre = locate(args)

    cfg = pre["config"]
    guard = int(cfg["guard_days"])
    threshold = float(cfg["rmse_threshold_m"])
    max_fraction = float(cfg["max_interpolated_fraction"])
    if cfg.get("gap_scenario_days") != 30:
        raise ValueError("Esta version espera gap_scenario_days=30")
    if cfg.get("min_years") != 10:
        raise ValueError("Esta version espera min_years=10")
    if cfg.get("seed") != 42:
        raise ValueError("Esta version espera seed=42")
    if guard != 3:
        raise ValueError("Esta version espera guard_days=3")
    if int(cfg.get("max_masks_per_length", 0)) != 50:
        raise ValueError("Esta version espera max_masks_per_length=50")
    if list(cfg.get("budget_sensitivity", [])) != [10, 20, 30, 50]:
        raise ValueError("Esta version espera budget_sensitivity=[10,20,30,50]")
    if int(cfg.get("budget_resamples", 0)) != 100:
        raise ValueError("Esta version espera budget_resamples=100")
    if not math.isclose(max_fraction, 0.10):
        raise ValueError("Esta version espera max_interpolated_fraction=0.10")
    if not math.isclose(threshold, 0.20):
        raise ValueError("Esta version espera RMSE threshold=0.20 m")
    if not math.isclose(float(cfg.get("stability_tolerance_m", -1)), 0.02):
        raise ValueError("Esta version espera stability_tolerance_m=0.02 m")
    if cfg.get("method") != "pchip":
        raise ValueError("Esta version espera PCHIP")
    if cfg.get("reserved_evaluated") is not False:
        raise ValueError("La configuracion debe declarar reserved_evaluated=false antes de abrir la reserva")

    # The reserve budget is fixed before reserved errors are inspected.
    # It must have universal support for every selected segment requiring interpolation.
    support = pre["reserve_support_all_required"].get(str(RESERVE_BUDGET), {})
    expected_interp = int(pre["n_selected_requiring_interpolation"])
    if (int(support.get("n_support", -1)) != expected_interp
            or int(support.get("n_total", -1)) != expected_interp):
        raise ValueError(
            "The reserved mask budget does not have universal support for all "
            "selected segments requiring interpolation"
        )

    selected = list(readcsv(sel/"segmentos_seleccionados_pre_reserva.csv"))
    manifest = list(readcsv(sel/"manifiesto_ventanas_reserva_max.csv"))

    if len(selected) != int(pre["n_wells_selected_pre_reserve"]):
        raise ValueError("Selected-segment table does not match the pre-reserve summary")
    if len({r["monitoring_location_id"] for r in selected}) != len(selected):
        raise ValueError("There must be exactly one selected segment per well")

    masks_by_key = defaultdict(list)
    for m in manifest:
        if m["phase"] != "reservada":
            raise ValueError("El manifiesto pre-reserva contiene una ventana no reservada")
        rank = int(m["selection_rank"])
        if rank <= RESERVE_BUDGET:
            masks_by_key[(m["segment_id"], int(m["gap_length_days"]))].append(m)

    # Verificar antes de abrir valores que los 20 targets estan congelados.
    for s in selected:
        if int(s["missing_days"]) == 0:
            continue
        for L in relevant_lengths(int(s["max_gap_missing_days"])):
            rows = masks_by_key.get((s["segment_id"], L), [])
            ranks = sorted(int(r["selection_rank"]) for r in rows)
            if ranks != list(range(1, RESERVE_BUDGET+1)):
                raise ValueError(
                    f"{s['segment_id']} L={L}: no hay exactamente ranks 1..20 congelados"
                )

    spec = dict(
        version=VERSION,
        reserve_opened=True,
        reserve_budget_per_length=RESERVE_BUDGET,
        rmse_threshold_m=threshold,
        max_interpolated_fraction=max_fraction,
        method="pchip",
        guard_days=guard,
        selection_rule=pre["selection_rule"],
        no_alternate_segment_after_reserve_failure=True,
        inputs={
            "pre_reserve_summary": sha(sel/"resumen_pre_reserva.json"),
            "selected_segments": sha(sel/"segmentos_seleccionados_pre_reserva.csv"),
            "reserve_manifest_max": sha(sel/"manifiesto_ventanas_reserva_max.csv"),
            "audit_summary": sha(audit/"resumen.json"),
        },
        script_sha256=sha(Path(__file__)),
        python=platform.python_version(),
        numpy=np.__version__,
        scipy=scipy.__version__,
    )
    token = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    root = ex/"08_validacion_reserva_final_v2"/("reserva_"+token)
    root.mkdir(parents=True, exist_ok=True)

    # Marca temprana: a partir de aqui la reserva se considera ABIERTA.
    writejson(root/"resumen_reserva.json", dict(
        etapa_completa=False,
        inicio_utc=now(),
        real_gaps_interpolated=False,
        **spec,
    ))

    reports = {}
    technical_errors = []

    for i, s in enumerate(selected, 1):
        sid = s["segment_id"]
        mid = s["monitoring_location_id"]
        folder = root/"por_segmento"/sid
        checkpoint = folder/"checkpoint.json"

        # Segmentos completos: no necesitan abrir valores reservados para
        # interpolacion; se aceptan directamente.
        if int(s["missing_days"]) == 0:
            reports[sid] = dict(
                monitoring_location_id=mid,
                segment_id=sid,
                etapa_completa=True,
                reserve_status="sin_huecos_no_requiere_validacion",
                final_status="aceptado_sin_interpolacion",
                max_reserved_rmse_m="",
                n_required_lengths=0,
                source_sha256=s["segment_sha256_v2"],
                output_hashes={},
            )
            continue

        required = relevant_lengths(int(s["max_gap_missing_days"]))
        chosen_manifest = []
        for L in required:
            chosen_manifest.extend(
                sorted(
                    masks_by_key[(sid, L)],
                    key=lambda r: int(r["selection_rank"])
                )
            )
        manifest_digest = hashlib.sha256(
            json.dumps(chosen_manifest, sort_keys=True).encode()
        ).hexdigest()

        try:
            saved = readjson(checkpoint) if checkpoint.exists() else {}
            reusable = (
                saved.get("etapa_completa")
                and saved.get("source_sha256") == s["segment_sha256_v2"]
                and saved.get("manifest_digest") == manifest_digest
                and saved.get("script_sha256") == spec["script_sha256"]
                and saved.get("reserve_budget_per_length") == RESERVE_BUDGET
                and all(
                    (folder/name).exists() and sha(folder/name) == digest
                    for name, digest in saved.get("output_hashes", {}).items()
                )
            )
            if reusable:
                reports[sid] = saved
            else:
                by_date = read_segment(audit, s)
                mask_results = []
                day_results = []

                for m in chosen_manifest:
                    L = int(m["gap_length_days"])
                    mr, dr = evaluate_mask(mid, sid, L, m, by_date, guard)
                    mask_results.append(mr)
                    day_results.extend(dr)

                # Agregar por longitud.
                length_rows = []
                max_rmse = -math.inf
                pass_all = True

                for L in required:
                    mrs = [r for r in mask_results if int(r["gap_length_days"]) == L]
                    drs = [r for r in day_results if int(r["gap_length_days"]) == L]
                    if len(mrs) != RESERVE_BUDGET:
                        raise ValueError(f"{sid} L={L}: se evaluaron {len(mrs)} mascaras")
                    agg = aggregate_length(mrs, drs, threshold)
                    max_rmse = max(max_rmse, float(agg["rmse_m"]))
                    pass_all = pass_all and bool(agg["pass_rmse"])
                    length_rows.append(dict(
                        monitoring_location_id=mid,
                        site_no=s["site_no"],
                        estado=s["estado"],
                        segment_id=sid,
                        gap_length_days=L,
                        **agg,
                    ))

                # La fraccion ya fue congelada antes de reserva, pero se verifica.
                fraction_ok = float(s["fraction_to_interpolate"]) <= max_fraction
                final_pass = pass_all and fraction_ok
                reserve_status = (
                    "pass_reserva" if final_pass
                    else "fail_reserva_rmse" if not pass_all
                    else "fail_fraccion_interpolada"
                )
                final_status = (
                    "aceptado_para_interpolacion"
                    if final_pass
                    else "excluido_tras_reserva"
                )

                folder.mkdir(parents=True, exist_ok=True)
                p_masks = folder/"metricas_mascaras_reserva.csv"
                p_days = folder/"errores_dia_reserva.csv.gz"
                p_lengths = folder/"metricas_longitud_reserva.csv"

                writecsv(p_masks, mask_results, MASK_FIELDS)
                write_gz(p_days, day_results, DAY_FIELDS)
                writecsv(p_lengths, length_rows, LENGTH_FIELDS)

                outputs = [p_masks, p_days, p_lengths]
                report = dict(
                    monitoring_location_id=mid,
                    segment_id=sid,
                    etapa_completa=True,
                    reserve_status=reserve_status,
                    final_status=final_status,
                    max_reserved_rmse_m=max_rmse,
                    n_required_lengths=len(required),
                    source_sha256=s["segment_sha256_v2"],
                    manifest_digest=manifest_digest,
                    reserve_budget_per_length=RESERVE_BUDGET,
                    script_sha256=spec["script_sha256"],
                    output_hashes={p.name: sha(p) for p in outputs},
                )
                writejson(checkpoint, report)
                reports[sid] = report

        except Exception as exc:
            technical_errors.append(dict(
                monitoring_location_id=mid,
                segment_id=sid,
                error=str(exc),
            ))

        if i % 20 == 0 or i == len(selected):
            print(
                f"Validacion reserva: {i}/{len(selected)}; "
                f"errores tecnicos {len(technical_errors)}",
                flush=True
            )

    if technical_errors:
        writecsv(
            root/"errores_tecnicos.csv",
            technical_errors,
            ["monitoring_location_id", "segment_id", "error"]
        )
        raise ValueError(
            f"Hay {len(technical_errors)} errores tecnicos. "
            "La reserva ya fue abierta; corregir SOLO errores tecnicos y reanudar "
            "sin cambiar criterios, seleccion ni mascaras."
        )

    # Construir tabla final pre-interpolacion.
    final_rows = []
    length_all = []

    for s in selected:
        sid = s["segment_id"]
        rep = reports.get(sid)
        if not rep or not rep.get("etapa_completa"):
            raise ValueError(f"Falta reporte completo para {sid}")

        if int(s["missing_days"]) > 0:
            p = root/"por_segmento"/sid/"metricas_longitud_reserva.csv"
            length_all.extend(readcsv(p))

        final_rows.append(dict(
            monitoring_location_id=s["monitoring_location_id"],
            site_no=s["site_no"],
            estado=s["estado"],
            segment_id=sid,
            origin_gap30=s["origin_gap30"],
            duration_years_approx=s["duration_years_approx"],
            date_min=s["date_min"],
            date_max=s["date_max"],
            completeness=s["completeness"],
            n_days=s["n_days"],
            expected_days=s["expected_days"],
            missing_days=s["missing_days"],
            fraction_to_interpolate=s["fraction_to_interpolate"],
            max_gap_missing_days=s["max_gap_missing_days"],
            development_status=s["development_status"],
            reserve_status=rep["reserve_status"],
            final_status=rep["final_status"],
            n_required_lengths=rep["n_required_lengths"],
            max_reserved_rmse_m=rep["max_reserved_rmse_m"],
            fallback_after_reserve=0,
        ))

    p_final = root/"estado_final_pre_interpolacion.csv"
    p_lengths = root/"metricas_reserva_segmento_longitud.csv"
    p_errors = root/"errores_tecnicos.csv"

    writecsv(p_final, final_rows, FINAL_FIELDS)
    writecsv(p_lengths, length_all, LENGTH_FIELDS)
    writecsv(
        p_errors,
        technical_errors,
        ["monitoring_location_id", "segment_id", "error"]
    )

    counts = Counter(r["final_status"] for r in final_rows)
    reserve_counts = Counter(r["reserve_status"] for r in final_rows)
    accepted = [
        r for r in final_rows
        if r["final_status"] in {
            "aceptado_para_interpolacion",
            "aceptado_sin_interpolacion",
        }
    ]

    summary_core = dict(
        etapa_completa=True,
        reserve_opened=True,
        reserved_values_evaluated=True,
        real_gaps_interpolated=False,
        reserve_budget_per_length=RESERVE_BUDGET,
        n_wells_entering_reserve=len(selected),
        n_without_interpolation=sum(int(r["missing_days"]) == 0 for r in final_rows),
        n_evaluated_in_reserve=sum(int(r["missing_days"]) > 0 for r in final_rows),
        reserve_statuses=dict(sorted(reserve_counts.items())),
        final_statuses=dict(sorted(counts.items())),
        n_wells_final_accepted=len(accepted),
        n_wells_final_excluded=len(final_rows)-len(accepted),
        n_final_to_interpolate=sum(
            r["final_status"] == "aceptado_para_interpolacion"
            for r in final_rows
        ),
        n_final_without_interpolation=sum(
            r["final_status"] == "aceptado_sin_interpolacion"
            for r in final_rows
        ),
        no_alternate_segment_after_reserve_failure=True,
        technical_errors=0,
    )

    outputs = [p_final, p_lengths, p_errors]
    # Build the final summary without duplicate-key expansion. The historical
    # run required a separate administrative close script after this point;
    # this public version integrates that non-scientific repair directly.
    final_summary = dict(spec)
    final_summary.update(summary_core)
    final_summary.update(
        finalizado_utc=now(),
        output_hashes={p.name: sha(p) for p in outputs},
        note=(
            "Reserved validation evaluated once with 20 masks per relevant length. "
            "No alternate segments were tested and natural gaps were not interpolated."
        ),
    )
    writejson(root/"resumen_reserva.json", final_summary)

    pointer = ex/"08_validacion_reserva_final_v2"/"ultima_reserva.txt"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    tmp = pointer.with_suffix(".tmp")
    tmp.write_text(str(root)+"\n", encoding="utf-8")
    tmp.replace(pointer)

    print("\nVALIDACION DE RESERVA COMPLETA")
    print(json.dumps(summary_core, indent=2, ensure_ascii=False))
    print("Resultados:", root)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nInterrumpido. Si la carpeta de salida ya existe, la reserva se considera ABIERTA. "
            "Reanuda con el mismo comando sin cambiar criterios.",
            file=sys.stderr
        )
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
