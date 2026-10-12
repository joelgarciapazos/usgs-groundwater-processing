from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = [
    REPO / "scripts" / f"{i:02d}_{name}"
    for i, name in [
        (1, "download_usgs.py"),
        (2, "normalize.py"),
        (3, "quality_control.py"),
        (4, "select_unconfined.py"),
        (5, "prepare_continuity.py"),
        (6, "gap_sensitivity_and_segmentation.py"),
        (7, "build_development_masks.py"),
        (8, "validate_development.py"),
        (9, "select_and_freeze_reserve.py"),
        (10, "validate_reserved_data.py"),
        (11, "interpolate_and_export.py"),
    ]
]

def test_json_configs_parse():
    for path in sorted((REPO / "config").glob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))

def test_entrypoints_help():
    paths = SCRIPTS + [REPO / "analysis" / "compare_interpolation_methods.py", REPO / "audit" / "verify_release_v1.py"]
    for path in paths:
        result = subprocess.run([sys.executable, str(path), "--help"], cwd=REPO, capture_output=True, text=True)
        assert result.returncode == 0, f"{path.name}: {result.stderr or result.stdout}"
