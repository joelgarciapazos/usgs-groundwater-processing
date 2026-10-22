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

The generator script matching the script hash above and the four frozen input files matching the recorded input hashes are not currently present in this GitHub repository. They are therefore still required before the manuscript comparison can be independently rerun from the repository.

Until those files are supplied, no replacement implementation should be presented as the historical generator and the exploratory pilot should remain clearly labelled as a different analysis.
