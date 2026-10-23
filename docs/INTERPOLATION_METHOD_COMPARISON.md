# Interpolation-method comparison evidence

## Two different analyses

The repository contains two conceptually different interpolation-method comparisons and they must not be conflated.

### Historical exploratory pilot

`analysis/compare_interpolation_methods.py` is the public adaptation of the historical `09_piloto_interpolacion.py` script recorded in `docs/provenance_mapping.csv`. It generates its own pilot/reserved masking windows and was retained as exploratory provenance. It is **not** the generator for the post hoc comparison reported in the ESSD manuscript and should not be used to reproduce manuscript Table A2.

### Post hoc paired comparison reported in the manuscript

After the production workflow and Version 1.0 membership were frozen, a separate read-only analysis compared linear interpolation, PCHIP, and Akima on the **exact frozen development masks**. The preserved completed-analysis summary records:

- 412 development segments;
- 82,750 unique frozen development masks;
- 248,250 method-mask evaluations;
- no reserved-data evaluation;
- no change to the production method or Version 1.0 membership;
- 1,655 segment-length units in the PCHIP reconciliation;
- exact reconciliation of PCHIP RMSE, MAE, and bias with the frozen development metrics at tolerance `1e-12`.

A verbatim copy of the available analysis summary is preserved as `docs/interpolation_method_comparison_summary.json`.

## Traceability recorded by the completed summary

The preserved summary identifies the generator and frozen inputs by SHA-256:

- generator script: `15336f81a050abb07e20de5c7b367332bc132768669b1dfad50743d5ed931960`;
- audit summary: `12b5043b2f2f9b1bf2f5b8193490fe29bdc92839097dd9f269776a96a565e5f6`;
- development summary: `a1f2649088deafffdebbaec950b52bc321444fea51dd6cc2715bc13d0179deb8`;
- frozen development masks: `5cc35f2d82a8cb34e413d70e1aac36e178b1b2d34b5d15a5f97089ff360d83d1`;
- frozen PCHIP metrics: `1ce3e7da0baef6adfaa334ca0abe89b6961e54d64dae36cee89d68808f27f49f`.

The summary also records SHA-256 hashes for all generated comparison outputs.

## Material still required for independent rerun

The historical generator has now been recovered and preserved as `analysis/historical/09_piloto_interpolacion_v3_frozen_masks.py`. After normalizing the supplied text to UTF-8 with LF line endings and a final newline, its SHA-256 is exactly:

`15336f81a050abb07e20de5c7b367332bc132768669b1dfad50743d5ed931960`

This matches the `script_sha256` recorded by the completed analysis summary. The recovered file is therefore identified as the actual historical generator for manuscript Table A2.

For convenient execution in the reorganized public repository, `analysis/compare_interpolation_methods_frozen_masks.py` is a minimal public-path adaptation. The scientific calculations and validation safeguards are unchanged; only the common-module import/path and usage text are adapted. Its SHA-256 is:

`b92864750640a749da3633592eb42ccf46acf604d7a3f641053a95569722ff47`

The exact historical generator is retained separately so the original recorded hash remains independently verifiable.

## Recovered frozen inputs and remaining dependencies

The four central frozen files identified in the completed Table A2 summary have now been recovered and independently SHA-256 verified:

- audit `resumen.json`: `12b5043b2f2f9b1bf2f5b8193490fe29bdc92839097dd9f269776a96a565e5f6`;
- development `resumen.json`: `a1f2649088deafffdebbaec950b52bc321444fea51dd6cc2715bc13d0179deb8`;
- `manifiesto_ventanas_desarrollo.csv`: `5cc35f2d82a8cb34e413d70e1aac36e178b1b2d34b5d15a5f97089ff360d83d1`;
- `metricas_segmento_longitud.csv`: `1ce3e7da0baef6adfaa334ca0abe89b6961e54d64dae36cee89d68808f27f49f`.

The recovered development-mask manifest contains 82,750 masks, matching both the audit summary and the completed Table A2 analysis. The recovered frozen PCHIP metrics file contains 1,655 segment-length rows, matching the development summary.

These four files are necessary but are not the complete input tree required by the exact historical generator. The script validates every file listed in the audit and development summaries before running, and it reads the frozen segment files referenced by `segmentos_elegibles_30d.csv`.

For an exact rerun, the original audit directory must therefore also provide:

- `segmentos_elegibles_30d.csv`;
- `auditoria_ventanas.csv`;
- `resumen_presupuestos.csv`;
- `errores.csv`;
- `auditoria_flujo.json`;
- the 421 frozen segment files recorded in `segment_files_hash_manifest`.

The original development directory must also provide:

- `estado_segmentos.csv`;
- `sensibilidad_presupuesto.csv`;
- `resumen_sensibilidad_presupuesto.csv`;
- `errores.csv`.

The recovered `manifiesto_ventanas_desarrollo.csv`, `metricas_segmento_longitud.csv`, and both summaries should remain with their original audit/development directories. The cleanest reproducibility package is therefore the complete frozen `auditoria_*` and `desarrollo_*` directories rather than a loose collection of individual files.

Table A2 cannot yet be independently regenerated from the GitHub checkout alone until those complete frozen directories, or an equivalent archived bundle preserving their hashes and paths, are made available.
