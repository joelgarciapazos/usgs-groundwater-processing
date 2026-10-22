from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_script(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


M07 = load_script("07_build_development_masks.py", "public_stage07")
M08 = load_script("08_validate_development.py", "public_stage08")
M09 = load_script("09_select_and_freeze_reserve.py", "public_stage09")
M10 = load_script("10_validate_reserved_data.py", "public_stage10")
M11 = load_script("11_interpolate_and_export.py", "public_stage11")


def write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "validation.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_frozen_validation_config_is_enforced(tmp_path):
    base = json.loads((REPO / "config" / "validation.json").read_text(encoding="utf-8"))
    for module in (M07, M08, M09):
        assert module.configuration(write_config(tmp_path, base.copy()))["guard_days"] == 3

    changed_guard = base.copy()
    changed_guard["guard_days"] = 4
    for module in (M07, M08, M09):
        with pytest.raises(ValueError):
            module.configuration(write_config(tmp_path, changed_guard))

    changed_budget = base.copy()
    changed_budget["max_masks_per_length"] = 51
    for module in (M07, M08, M09):
        with pytest.raises(ValueError):
            module.configuration(write_config(tmp_path, changed_budget))


def test_development_requires_exact_frozen_50_mask_panel():
    rows = []
    for length in (1, 3):
        rows.extend(
            {"gap_length_days": str(length), "selection_rank": str(rank)}
            for rank in range(1, 51)
        )
    assert M08.development_panel_complete(rows, [1, 3]) is True
    assert M08.development_panel_complete(rows[:-1], [1, 3]) is False


def test_required_gap_lengths_stop_at_30_days():
    assert M09.relevant_lengths(1) == [1]
    assert M09.relevant_lengths(8) == [1, 3, 7, 8]
    assert M09.relevant_lengths(30) == [1, 3, 7, 15, 30]
    with pytest.raises(ValueError):
        M09.relevant_lengths(31)


def test_reserved_rmse_threshold_is_applied_per_length():
    at_threshold = [{"n_test": 1, "sse_m2": 0.04, "sae_m": 0.2, "sum_error_m": 0.2}]
    days = [{"abs_error_m": 0.2}]
    result = M10.aggregate_length(at_threshold, days, 0.20)
    assert result["rmse_m"] == pytest.approx(0.20)
    assert result["pass_rmse"] == 1

    above_threshold = [{"n_test": 1, "sse_m2": 0.040401, "sae_m": 0.201, "sum_error_m": 0.201}]
    days2 = [{"abs_error_m": 0.201}]
    result2 = M10.aggregate_length(above_threshold, days2, 0.20)
    assert result2["rmse_m"] == pytest.approx(0.201)
    assert result2["pass_rmse"] == 0


def test_empty_result_helpers_and_hash_guard(tmp_path):
    assert M09.optional_float("") is None
    assert M09.optional_float(None) is None
    assert M11.ratio_or_none(0, 0) is None
    assert M11.aggregate_or_none([], min) is None

    p = tmp_path / "x.txt"
    p.write_text("frozen", encoding="utf-8")
    digest = M11.sha(p)
    M11.require_file_hash(p, digest, "mismatch")
    with pytest.raises(ValueError, match="mismatch"):
        M11.require_file_hash(p, "0" * 64, "mismatch")


def test_natural_gap_does_not_require_symmetric_3x3_and_sources_are_preserved(tmp_path):
    audit = tmp_path / "audit"
    root = tmp_path / "out"
    seg = audit / "segments" / "seg.csv.gz"
    rows = [
        {"date": f"2020-01-{day:02d}", "depth_to_water_m": f"{float(day):.1f}"}
        for day in range(1, 12) if day != 3
    ]
    for row in rows:
        row.update(
            usable_input="1",
            interpolated_flag="0",
            source_estimated_effective="0",
            jump_alert="0",
            latitude="40.0",
            longitude="-105.0",
        )
    M11.writecsv(seg, rows, list(rows[0]))

    s = {
        "segment_id": "seg-test",
        "monitoring_location_id": "USGS-12345678",
        "site_no": "12345678",
        "estado": "CO",
        "date_min": "2020-01-01",
        "date_max": "2020-01-11",
        "expected_days": "11",
        "missing_days": "1",
        "max_gap_missing_days": "1",
        "origin_gap30": "directo",
        "duration_years_approx": "0.027379",
        "segment_file_v2": str(seg.relative_to(audit)),
        "segment_sha256_v2": M11.sha(seg),
    }
    final_state = {
        "missing_days": "1",
        "reserve_status": "pass_reserva",
        "final_status": "aceptado_para_interpolacion",
    }
    well = {
        "monitoring_location_id": "USGS-12345678",
        "site_no": "12345678",
        "latitude": "40.0",
        "longitude": "-105.0",
    }

    meta, gaps = M11.process_well(audit, root, s, final_state, well)
    assert meta["n_source_values"] == 10
    assert meta["n_interpolated_values"] == 1
    assert len(gaps) == 1
    assert gaps[0]["missing_days"] == 1
    assert gaps[0]["symmetric_3x3_context_available"] == 0

    output_rows = list(M11.readcsv(root / meta["series_file"]))
    by_date = {row["date"]: row for row in output_rows}
    for source in rows:
        out = by_date[source["date"]]
        assert out["groundwater_depth_m"] == source["depth_to_water_m"]
        assert out["observed_flag"] == "1"
        assert out["interpolated_flag"] == "0"
    assert by_date["2020-01-03"]["observed_flag"] == "0"
    assert by_date["2020-01-03"]["interpolated_flag"] == "1"


def test_final_export_handles_zero_accepted_wells_and_reports_actual_exclusions(tmp_path):
    import subprocess

    qa = (tmp_path / "qa").resolve()
    ex = (tmp_path / "experiment").resolve()
    audit = ex / "05_validacion_segmentos_v2" / "audit_test"
    sel = ex / "07_seleccion_pre_reserva_v2" / "selection_test"
    reserve = ex / "08_validacion_reserva_final_v2" / "reserve_test"

    inventory = qa / "03_libres" / "pozos_libres_candidatos.csv"
    M11.writecsv(
        inventory,
        [{
            "monitoring_location_id": "USGS-12345678",
            "site_no": "12345678",
            "latitude": "40.0",
            "longitude": "-105.0",
        }],
        ["monitoring_location_id", "site_no", "latitude", "longitude"],
    )
    M11.writejson(ex / "manifiesto.json", {
        "carpeta_qa": str(qa),
        "inventory_sha256": M11.sha(inventory),
    })

    M11.writejson(audit / "resumen.json", {
        "etapa_completa": True,
        "reserved_evaluated": False,
    })
    (ex / "05_validacion_segmentos_v2").mkdir(parents=True, exist_ok=True)
    (ex / "05_validacion_segmentos_v2" / "ultima_auditoria.txt").write_text(
        str(audit) + "\n", encoding="utf-8"
    )

    selected_path = sel / "segmentos_seleccionados_pre_reserva.csv"
    M11.writecsv(
        selected_path,
        [{
            "monitoring_location_id": "USGS-12345678",
            "site_no": "12345678",
            "segment_id": "seg-test",
        }],
        ["monitoring_location_id", "site_no", "segment_id"],
    )
    reserve_manifest = sel / "manifiesto_ventanas_reserva_max.csv"
    M11.writecsv(reserve_manifest, [], ["segment_id"])
    pre_summary = sel / "resumen_pre_reserva.json"
    M11.writejson(pre_summary, {
        "etapa_completa": True,
        "inputs": {"audit_summary": M11.sha(audit / "resumen.json")},
        "output_hashes": {
            selected_path.name: M11.sha(selected_path),
            reserve_manifest.name: M11.sha(reserve_manifest),
        },
    })
    (ex / "07_seleccion_pre_reserva_v2").mkdir(parents=True, exist_ok=True)
    (ex / "07_seleccion_pre_reserva_v2" / "ultima_seleccion.txt").write_text(
        str(sel) + "\n", encoding="utf-8"
    )

    final_state = reserve / "estado_final_pre_interpolacion.csv"
    M11.writecsv(
        final_state,
        [{
            "monitoring_location_id": "USGS-12345678",
            "site_no": "12345678",
            "estado": "CO",
            "segment_id": "seg-test",
            "missing_days": "1",
            "reserve_status": "fail_reserva_rmse",
            "final_status": "excluido_tras_reserva",
            "max_reserved_rmse_m": "0.25",
        }],
        [
            "monitoring_location_id", "site_no", "estado", "segment_id",
            "missing_days", "reserve_status", "final_status", "max_reserved_rmse_m",
        ],
    )
    reserve_summary = reserve / "resumen_reserva.json"
    M11.writejson(reserve_summary, {
        "etapa_completa": True,
        "reserve_opened": True,
        "reserved_values_evaluated": True,
        "real_gaps_interpolated": False,
        "technical_errors": 0,
        "n_wells_final_accepted": 0,
        "n_wells_final_excluded": 1,
        "n_final_to_interpolate": 0,
        "n_final_without_interpolation": 0,
        "inputs": {
            "pre_reserve_summary": M11.sha(pre_summary),
            "selected_segments": M11.sha(selected_path),
            "reserve_manifest_max": M11.sha(reserve_manifest),
            "audit_summary": M11.sha(audit / "resumen.json"),
        },
        "output_hashes": {final_state.name: M11.sha(final_state)},
    })
    (ex / "08_validacion_reserva_final_v2").mkdir(parents=True, exist_ok=True)
    (ex / "08_validacion_reserva_final_v2" / "ultima_reserva.txt").write_text(
        str(reserve) + "\n", encoding="utf-8"
    )

    result = subprocess.run(
        [
            sys.executable, str(SCRIPTS / "11_interpolate_and_export.py"),
            "--carpeta", str(qa), "--experimento", str(ex), "--reserva", str(reserve),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    pointer = ex / "09_dataset_final_v2" / "ultimo_dataset.txt"
    output = Path(pointer.read_text(encoding="utf-8").strip())
    summary = M11.readjson(output / "resumen_dataset_final.json")
    assert summary["n_final_wells"] == 0
    assert summary["excluded_after_reserve"] == 1
    assert summary["global_interpolated_fraction"] is None
    assert summary["temporal_coverage_min"] is None
    assert summary["median_segment_duration_years"] is None


def test_equivalence_audit_detects_and_accepts_rows(tmp_path):
    import importlib.util
    audit_path = REPO / "audit" / "compare_historical_public.py"
    spec = importlib.util.spec_from_file_location("equiv_audit", audit_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    text = "monitoring_location_id,segment_id,gap_length_days,rmse_m\nUSGS-1,s1,3,0.1\n"
    a.write_text(text, encoding="utf-8")
    b.write_text(text, encoding="utf-8")
    assert mod.compare_table(a, b, 1e-12)["ok"]
    b.write_text(text.replace("0.1", "0.1000000000005"), encoding="utf-8")
    assert mod.compare_table(a, b, 1e-12)["ok"]
    b.write_text(text.replace("0.1", "0.11"), encoding="utf-8")
    assert not mod.compare_table(a, b, 1e-12)["ok"]


def test_development_stage_exposes_frozen_relevant_lengths():
    """Regression test for the stage-08 helper required by the real workflow."""
    assert M08.relevant_lengths(0) == []
    assert M08.relevant_lengths(1) == [1]
    assert M08.relevant_lengths(8) == [1, 3, 7, 8]
    assert M08.relevant_lengths(30) == [1, 3, 7, 15, 30]
    with pytest.raises(ValueError):
        M08.relevant_lengths(31)


def test_duplicate_barrier_auditor_uses_actual_prepared_stage_name():
    """Regression test for the historical 01_preparados directory name."""
    auditor = REPO / "audit" / "check_duplicate_barrier_impact.py"
    text = auditor.read_text(encoding="utf-8")
    assert "01_preparados" in text
    assert "01_preparacion" not in text


