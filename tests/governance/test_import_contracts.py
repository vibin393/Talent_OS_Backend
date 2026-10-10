"""AD-32 import contracts (Epic 13, story 13.11): ``lint-imports`` keeps the Phase 2 layer DAG and keeps core /
Phase 1 infrastructure below the Phase 2 domains (``Backend/.importlinter``)."""

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def test_import_linter_contracts_kept() -> None:
    exe = Path(sys.executable).with_name("lint-imports.exe" if sys.platform == "win32" else "lint-imports")
    cmd = [str(exe)] if exe.exists() else [sys.executable, "-m", "importlinter.cli"]
    proc = subprocess.run(cmd, cwd=BACKEND, capture_output=True, text=True, check=False, timeout=600)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "broken" in proc.stdout and " 0 broken" in proc.stdout
