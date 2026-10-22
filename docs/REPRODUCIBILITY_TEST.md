# Reproducibility and scientific-equivalence test

## Purpose

This repository is a reorganized public implementation of the frozen workflow used to generate Version 1.0 of the groundwater-depth dataset.

A national reproduction test was performed to verify that the public reorganization did not alter the scientific results.

## Scope

The historical and public workflows were run from the same frozen post-QA unconfined-well input (`03_libres`).

The comparison covers:

- continuity preparation;
- 15/30/45-day gap sensitivity;
- temporal segmentation;
- development-mask construction;
- PCHIP development validation;
- deterministic segment selection;
- reserved validation;
- reconstruction of natural internal gaps;
- final dataset export.

This test does not represent a new download from USGS NWIS or an independent rerun of acquisition, normalization, QA, and aquifer-selection stages 01–04. Development and reserved targets are temporally separated within the same monitoring records and should not be interpreted as statistically independent groundwater observations.

## Frozen protocol

The reproduced workflow used:

- USGS parameter `72019`;
- daily mean statistic `00003`;
- Approved records only;
- unconfined aquifers (`U`) only;
- minimum segment duration of 10 years;
- maximum internal gap of 30 days;
- PCHIP interpolation;
- no extrapolation;
- exactly 50 development masks per required gap length;
- exactly 20 reserved masks per required gap length;
- 3 eligible source observations on each side of artificial validation masks;
- RMSE <= 0.20 m at every required gap length;
- no alternative segment after reserved-validation failure.

The 3+3 support requirement applies to artificial validation masks, not to natural gaps reconstructed in the final dataset.

## National reproduction

Historical experiment:

`exp_4acf1c339756ad60`

Public reproduction:

`exp_3679d6823ee4a316`

The public workflow reproduced:

- 331 final wells;
- 94 direct-continuity wells;
- 237 wells recovered by segmentation;
- 8 series without interpolation;
- 323 series with interpolation;
- 2,357,801 retained source values;
- 16,311 interpolated values;
- 2,374,112 total daily values;
- 3,845 reconstructed natural gaps;
- maximum reconstructed gap of 30 days;
- global interpolated fraction of 0.6870%;
- 3 wells excluded after reserved validation;
- 0 technical errors;
- temporal coverage from 1942-08-14 to 2025-12-31.

Reserved validation reproduced:

- 334 wells selected before reserve evaluation;
- 326 evaluated in reserved validation;
- 323 passed;
- 3 failed;
- 8 required no interpolation;
- 331 final accepted wells.

## Direct historical-public comparison

The read-only auditor `audit/compare_historical_public.py` compared the historical and public scientific outputs with an absolute numerical tolerance of `1e-12`.

All compared products passed, including eligible segments, development masks and metrics, segment-selection decisions, reserved masks and metrics, final metadata, reconstructed gaps, post-reserve exclusions, and the complete final daily dataset.

Final result:

`SCIENTIFIC EQUIVALENCE: PASS`

The row-level comparison covered all 2,374,112 exported daily records.

## Re-running the equivalence comparison

The auditor compares two already-generated experiment directories. With the frozen historical and public experiments available locally, run:

```bash
python audit/compare_historical_public.py \
  /path/to/exp_4acf1c339756ad60 \
  /path/to/exp_3679d6823ee4a316 \
  --atol 1e-12 \
  --report /path/to/equivalence_report.json
```

The two experiment directories must retain their stage pointer files and the scientific outputs listed by the auditor. To regenerate the public experiment itself rather than only compare two existing runs, the same frozen post-QA input directory (`03_libres`) used by the historical run is also required.

## Evidence availability

The following evidence is present in the public repository:

- `audit/compare_historical_public.py`, the read-only comparison code;
- this document, which records the historical/public experiment identifiers, counts, tolerance, and PASS result;
- `docs/duplicate_barrier_diagnostic.json`, the preserved duplicate-barrier diagnostic;
- `docs/provenance_mapping.csv`, which maps historical scripts to the public `v1.0.0` scripts and their SHA-256 hashes.

The following items required for a third party to rerun the national equivalence audit are **not currently distributed in the repository**:

- the frozen historical experiment directory `exp_4acf1c339756ad60`;
- the public reproduction directory `exp_3679d6823ee4a316`;
- the shared frozen post-QA `03_libres` input tree used to generate those experiments;
- a committed copy of the generated per-table JSON report from `--report`;
- a repository-level manifest giving independent SHA-256 identifiers for the frozen national input tree.

Therefore the repository documents and tests the comparison logic and records the completed result, but the full national equivalence audit cannot be independently rerun from the GitHub checkout alone until those frozen inputs/evidence files are made available.

## Additional diagnostic and regression tests

The duplicate-barrier diagnostic found zero occurrences of `identical_value_duplicates_collapsed` in the prepared historical continuity input used for the frozen final experiment.

Its output is preserved in:

`docs/duplicate_barrier_diagnostic.json`

During the real reproduction run, two publication-code packaging defects were identified and corrected:

1. the duplicate-barrier auditor referenced `01_preparacion` instead of `01_preparados`;
2. `scripts/08_validate_development.py` lacked `BASE_LENGTHS` and the `relevant_lengths()` helper.

Neither defect affected the historical dataset or its scientific results.

Regression tests were added for both cases.

Current test-suite status:

`12 passed`

## Interpretation

`SCIENTIFIC EQUIVALENCE: PASS` means that the reorganized public workflow reproduced the frozen historical scientific outputs from the same post-QA input, including the complete final daily dataset, within the stated tolerance.

It does not imply that a future retrieval from the live USGS NWIS service will necessarily be byte-for-byte identical, because upstream source data or service responses may change over time.
