# Repository audit for ESSD submission — 2026-10-22

## Frozen baseline

The audit began from Git commit `907769d59d9bb141c8be6ed0a67ec585c84df41e`. At audit start, both `main` and annotated tag `v1.0.0` resolved to this commit. The tag was not moved or modified.

## Provenance-hash verification

Every public-file SHA-256 recorded in `docs/provenance_mapping.csv` was recomputed against the files at the frozen baseline. All 14 recorded public hashes matched, including workflow stages 01–11, `qa_common.py`, `continuity_common.py`, and the historical exploratory interpolation pilot.

No file under `scripts/` or `analysis/` is modified by this audit branch, so the historical `v1.0.0` provenance mapping remains valid.

## Verified documentation issues

- `docs/PUBLIC_ADAPTATION.md` still described the national equivalence test as pending, while the README and `docs/REPRODUCIBILITY_TEST.md` record the completed PASS result.
- README stage 10 used the phrase “independent reserved validation”, which can imply statistical independence not supported for autocorrelated observations from the same wells.
- README still used provisional “repository candidate” language and a future software-DOI statement not aligned with the manuscript's current code-availability wording.
- The repository's `analysis/compare_interpolation_methods.py` is the historical exploratory pilot and does not generate the manuscript's final 82,750-mask paired comparison.
- Two regression-test functions were duplicated verbatim in `tests/test_scientific_invariants.py`.

## Dataset/software status

The manuscript treats dataset DOI `10.5281/zenodo.23289430` as a reserved DOI for an unpublished Version 1.0 peer-review draft. The GitHub software release is a separate product. The manuscript-cited software version remains `v1.0.0`, which has no separate software DOI.

## Outstanding evidence

The national historical-public equivalence result is documented, but the frozen experiment directories, shared national `03_libres` input tree, a generated full JSON equivalence report, and a repository-level input-tree checksum manifest are not distributed here.

For the manuscript's post hoc interpolation comparison, the completed summary and the exact historical generator are now preserved. The recovered generator matches the recorded script SHA-256 exactly. The remaining missing material for an independent rerun is the set of four frozen input files/directories identified by hashes in `docs/INTERPOLATION_METHOD_COMPARISON.md`.
