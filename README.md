# USGS groundwater-depth dataset processing workflow

This repository contains the compact public processing workflow used to construct the Version 1.0 groundwater-depth dataset derived from USGS NWIS daily values. The public implementation preserves the scientific rules of the frozen workflow while removing user-specific filesystem paths and integrating two administrative JSON-closing repairs that did not alter calculations.

## Scope

The workflow retrieves USGS parameter `72019`, normalizes supported units to metres, restricts the QA stream to daily mean statistic `00003` with `Approved` status, retains aquifer type `U`, applies temporal segmentation with a minimum 10-year span and a selected 30-day maximum internal gap, validates PCHIP with temporally separated development and reserved masks, and exports accepted daily records with explicit observed/interpolated provenance. The frozen final protocol requires exactly 50 development masks for every relevant gap length and 20 reserved masks per relevant gap length, with three eligible source observations on each side of artificial masks and RMSE <=0.20 m at every required length. The 3+3 support rule is a validation-mask rule and is not an additional requirement for natural gaps reconstructed in the final stage.

## Main workflow

Run the scripts in `scripts/` in numerical order. The main stages are:

1. `01_download_usgs.py` — immutable/resumable USGS API download.
2. `02_normalize.py` — normalization and unit conversion while retaining invalid rows for audit.
3. `03_quality_control.py` — Approved daily-mean QA, duplicate handling, series selection, and diagnostic flags.
4. `04_select_unconfined.py` — select USGS aquifer type `U`.
5. `05_prepare_continuity.py` — convert QA flags into continuity/validation policies.
6. `06_gap_sensitivity_and_segmentation.py` — compare 15/30/45-day thresholds and export temporal segments.
7. `07_build_development_masks.py` — rebuild all eligible 30-day segments and freeze development masking windows.
8. `08_validate_development.py` — PCHIP development validation and masking-budget sensitivity.
9. `09_select_and_freeze_reserve.py` — select one segment per well and freeze reserved windows.
10. `10_validate_reserved_data.py` — reserved validation on target years temporally separated from development.
11. `11_interpolate_and_export.py` — PCHIP reconstruction of accepted natural gaps and final dataset export.

`analysis/compare_interpolation_methods.py` is the historical exploratory linear/PCHIP/Akima pilot retained for provenance. It generates its own pilot masking windows and is not the post hoc paired comparison reported in the manuscript. The actual historical generator used for manuscript Table A2 is preserved byte-equivalently (UTF-8/LF) as `analysis/historical/09_piloto_interpolacion_v3_frozen_masks.py`; its SHA-256 matches the hash recorded by the completed analysis. A public-path adaptation with unchanged scientific logic is provided as `analysis/compare_interpolation_methods_frozen_masks.py`. The manuscript comparison used the exact frozen development masks (82,750 masks from 412 development segments) and did not open reserved data. See `docs/INTERPOLATION_METHOD_COMPARISON.md`.

## Configuration

Study parameters are stored under `config/`:

- `qa.json` — parameter/statistic/aquifer scope and QA diagnostics.
- `continuity.json` — 10-year rule, 15/30/45-day sensitivity, qualifier/flag policies.
- `validation.json` — frozen 30-day PCHIP validation protocol.
- `release_v1_0_expected.json` — published-release counts for audit only; these are not general scientific thresholds.

## Paths

No personal absolute path is required. By default the download/QA workflow writes below `<repository>/data`. You can instead pass `--root /your/path` or define `USGS_GW_ROOT`. Continuity and validation scripts accept `--carpeta /path/to/qa_<identifier>`; `USGS_QA_DIR` provides a portable default. The workflow uses POSIX file locking (`fcntl`), so it is intended for Linux or WSL rather than native Windows Python.

For a reduced technical run, use `01_download_usgs.py --sitio USGS-<id>` so the downloaded inventory itself contains only the explicitly requested well. `--max-pozos` is a resumable download limit and can leave the inventory intentionally incomplete, which is not suitable as the starting point for QA. After stages 02-04, pass the generated `qa_<identifier>` directory explicitly with `--carpeta` to stages 05-11.

## Environment

The frozen final run used Python 3.10.12, NumPy 2.2.6, and SciPy 1.15.3. Install the numerical dependencies with:

```bash
python3 -m pip install -r requirements.txt
```

The downloader and initial QA utilities use only the Python standard library. The download script reads the USGS API key from `USGS_API_KEY` or requests it interactively; the key is not written to output files.

## Reproducibility note

This repository is a public adaptation of the research scripts, not a byte-identical copy of the historical source files. Changes are intentionally limited to portable paths, public filenames/documentation, import references required by those filename changes, explicit validation of the frozen configuration and provenance chain, robust handling of non-applicable/empty reduced-run summaries, and integration of the administrative close fixes previously performed by `17b_cerrar_reserva.py` and `18c_cerrar_dataset_final.py`. These changes are intended not to alter the scientific criteria or numerical methods. See `docs/provenance_mapping.csv` and `docs/PUBLIC_ADAPTATION.md`. Release-specific expected counts are verified separately with `audit/verify_release_v1.py`.

Because USGS can revise historical records, rerunning the API workflow at a later date may not reproduce byte-identical source data unless the original raw API responses are also preserved. The associated scientific dataset is a separate Zenodo product. The manuscript identifies dataset Version 1.0 as an unpublished peer-review draft with reserved DOI `10.5281/zenodo.23289430`; this repository does not describe that DOI as a published dataset record.

## Status

The tagged software release `v1.0.0` completed a national historical-versus-public scientific-equivalence test using the same frozen post-QA input (`03_libres`). The automated test suite includes scientific invariants for the frozen validation protocol, provenance checks, reduced-run edge cases, source-value preservation, and natural-gap reconstruction. The full historical-public comparison, including the 2,374,112-row final daily dataset, returned `SCIENTIFIC EQUIVALENCE: PASS` with an absolute numerical tolerance of `1e-12`. This establishes equivalence from the shared post-QA starting point; it is not an independent reproduction of acquisition, normalization, QA, or aquifer selection from a new USGS retrieval. Release-specific expected counts remain separate from scientific acceptance criteria.

## Scientific-equivalence audit

A national equivalence audit was completed with `audit/compare_historical_public.py` using the same frozen post-QA input as the historical workflow. The comparison covers scientific segment, mask, metric, selection, reserve, gap, metadata, exclusion, and final daily-data outputs while ignoring implementation-only paths, timestamps, run identifiers, and hashes. All compared products passed. The exact command, available evidence, and missing frozen inputs/report files are documented in `docs/REPRODUCIBILITY_TEST.md`.

A separate read-only diagnostic, `audit/check_duplicate_barrier_impact.py`, quantifies whether the historical `identical_value_duplicates_collapsed` QA flag became a continuity barrier and whether affected dates occur in selected final intervals. This diagnostic does not alter the frozen policy.

## License

The software is released under the MIT License. The scientific dataset is a separate research product and is distributed under its own data license.

## Citation

`CITATION.cff` contains citation metadata for software Version 1.0.0. For the manuscript, cite the frozen GitHub release at https://github.com/joelgarciapazos/usgs-groundwater-processing/releases/tag/v1.0.0. The canonical source repository is https://github.com/joelgarciapazos/usgs-groundwater-processing. The software and the Zenodo scientific dataset are separate products; no separate software DOI is currently assigned.
