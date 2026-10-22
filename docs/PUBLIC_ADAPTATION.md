# Public adaptation notes

The public `v1.0.0` adaptation was prepared from the frozen research scripts that produced the final dataset. The objective is to make the workflow understandable and portable without rewriting the scientific method.

## Deliberate changes

- Replaced user-specific absolute default paths with repository-relative defaults and environment-variable/CLI overrides.
- Renamed the public scripts in numerical workflow order and updated only the import/hash references required by those names.
- Added concise English module documentation for the public entry points. Internal field names and output-stage directory names are retained where changing them would increase the risk of altering provenance logic.
- Moved the historical linear/PCHIP/Akima pilot comparison to an optional `analysis/` entry point. This pilot generates its own masks and is distinct from the later post hoc paired comparison on the exact frozen development masks reported in the manuscript; see `INTERPOLATION_METHOD_COMPARISON.md`.
- Integrated the administrative summary-closing fixes from the historical `17b_cerrar_reserva.py` and `18c_cerrar_dataset_final.py` directly into public stages 10 and 11. Those fixes address duplicate-key construction of final JSON summaries and do not recalculate PCHIP, reopen reserved values, change selections, or alter final series.
- Kept the frozen study configuration values in JSON files and now reject changes to the final validation settings (30-day scenario, 10-year minimum, 3+3 artificial-mask support, 50 development masks, 20 reserved masks, PCHIP, RMSE 0.20 m, and 10% maximum interpolation fraction where applicable). Published-release counts are separated into `release_v1_0_expected.json` and are documented as audit references rather than general scientific thresholds.
- Removed the remaining release-specific `excluded_after_reserve=3` constant from the general exporter; the exporter now derives this count from the actual reserve result.
- Added safe handling for reduced runs with no interpolation candidates or zero accepted wells. Non-applicable aggregate statistics are written as JSON `null` instead of causing conversion, division, or empty-sequence errors.
- Strengthened cross-stage provenance checks so the final exporter rejects a selection, reserve, audit, or canonical inventory whose SHA-256 does not match the hashes recorded by the upstream run.
- Development segments with fewer than the frozen 50 masks at any required length are explicitly marked as insufficient/non-usable instead of being treated as a technical crash. This does not relax the historical 50-mask acceptance requirement.

## Not included in the main public pipeline

The diagnostic/exploratory scripts `05_revisar_resultados.py`, `06_auditar_marcas_qa.py`, `10_resumen_criterios.py`, `11_perturbacion_multiescala.py`, `12_criterios_diarios.py`, and the superseded `13_evaluacion_reservada.py` are not required for the final v2 construction path. The manuscript-support script `19_auditar_y_generar_resultados_essd.py` is also not required to construct the dataset.

## Validation status of this adaptation

The public adaptation was subsequently exercised in a national historical-versus-public equivalence test from the same frozen post-QA unconfined-well input (`03_libres`). Historical experiment `exp_4acf1c339756ad60` and public experiment `exp_3679d6823ee4a316` were compared with `audit/compare_historical_public.py` at an absolute numerical tolerance of `1e-12`. The comparison passed for the scientific segment, mask, metric, selection, reserve, gap, metadata, exclusion, and complete final daily-data products, including all 2,374,112 final rows.

This completed test supersedes the earlier note that equivalence testing was still pending. It establishes scientific equivalence from the shared frozen post-QA starting point. It does **not** represent an independent reproduction of stages 01–04 from a fresh USGS API retrieval. The frozen national input directory and the generated full JSON equivalence report are not currently distributed in this repository; their availability and the exact rerun command are documented in `REPRODUCIBILITY_TEST.md`.

The repository test suite also includes smoke checks and scientific-invariant tests, including a complete synthetic final-export case with zero accepted wells and a synthetic natural-gap reconstruction that verifies exact preservation of source values without imposing the artificial-mask 3+3 rule on the natural gap.

## Release-specific count checks

Exact counts from Dataset Version 1.0 (for example 331 final wells) are not scientific acceptance criteria in the public pipeline. They are stored in `config/release_v1_0_expected.json` and can be checked after reconstruction with `audit/verify_release_v1.py`. Core stages reconcile counts against their own upstream manifests and summaries instead.
