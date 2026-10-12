# Public adaptation notes

The public candidate was prepared from the frozen research scripts that produced the final dataset. The objective is to make the workflow understandable and portable without rewriting the scientific method.

## Deliberate changes

- Replaced user-specific absolute default paths with repository-relative defaults and environment-variable/CLI overrides.
- Renamed the public scripts in numerical workflow order and updated only the import/hash references required by those names.
- Added concise English module documentation for the public entry points. Internal field names and output-stage directory names are retained where changing them would increase the risk of altering provenance logic.
- Moved the linear/PCHIP/Akima pilot comparison to an optional `analysis/` entry point.
- Integrated the administrative summary-closing fixes from the historical `17b_cerrar_reserva.py` and `18c_cerrar_dataset_final.py` directly into public stages 10 and 11. Those fixes address duplicate-key construction of final JSON summaries and do not recalculate PCHIP, reopen reserved values, change selections, or alter final series.
- Kept the frozen study configuration values in JSON files and now reject changes to the final validation settings (30-day scenario, 10-year minimum, 3+3 artificial-mask support, 50 development masks, 20 reserved masks, PCHIP, RMSE 0.20 m, and 10% maximum interpolation fraction where applicable). Published-release counts are separated into `release_v1_0_expected.json` and are documented as audit references rather than general scientific thresholds.
- Removed the remaining release-specific `excluded_after_reserve=3` constant from the general exporter; the exporter now derives this count from the actual reserve result.
- Added safe handling for reduced runs with no interpolation candidates or zero accepted wells. Non-applicable aggregate statistics are written as JSON `null` instead of causing conversion, division, or empty-sequence errors.
- Strengthened cross-stage provenance checks so the final exporter rejects a selection, reserve, audit, or canonical inventory whose SHA-256 does not match the hashes recorded by the upstream run.
- Development segments with fewer than the frozen 50 masks at any required length are explicitly marked as insufficient/non-usable instead of being treated as a technical crash. This does not relax the historical 50-mask acceptance requirement.

## Not included in the main public pipeline

The diagnostic/exploratory scripts `05_revisar_resultados.py`, `06_auditar_marcas_qa.py`, `10_resumen_criterios.py`, `11_perturbacion_multiescala.py`, `12_criterios_diarios.py`, and the superseded `13_evaluacion_reservada.py` are not required for the final v2 construction path. The manuscript-support script `19_auditar_y_generar_resultados_essd.py` is also not required to construct the dataset.

## Validation status of this adaptation

All Python files in this candidate have been syntax-compiled and all JSON files parsed successfully. The repository test suite includes both smoke checks and scientific-invariant tests, including a complete synthetic final-export case with zero accepted wells and a synthetic natural-gap reconstruction that verifies exact preservation of source values without imposing the artificial-mask 3+3 rule on the natural gap. This environment does not contain the original full USGS raw/QA directory tree, so the adapted workflow has not yet been rerun end-to-end over the frozen national inputs. Before public release, the next required step is a reduced historical-versus-public equivalence test on frozen inputs, followed by a broader equivalence audit.

## Release-specific count checks

Exact counts from Dataset Version 1.0 (for example 331 final wells) are not scientific acceptance criteria in the public pipeline. They are stored in `config/release_v1_0_expected.json` and can be checked after reconstruction with `audit/verify_release_v1.py`. Core stages reconcile counts against their own upstream manifests and summaries instead.
