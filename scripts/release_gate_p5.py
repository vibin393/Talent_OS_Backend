# ruff: noqa: E501
"""Phase 5 release gate (Epic 79 story 79.7, FR-414, spec A9). CI job ``p5-gate`` -- all deterministic with Fakes.

Runs the egress suite, the MCP / portal red-team + leak suites, the campaign-copy evals, the Conformance Kit over
registered manifests, the Public API / MCP isolation suites, the secret scan (log + pattern), ``lint-imports`` and the
★ suite. Any failure fails the build. ``--dry-run`` lists the steps and exits 0 (for scheduled live runs without keys).

    ./venv/Scripts/python.exe scripts/release_gate_p5.py [--dry-run]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PY = sys.executable

# name -> command (run from BACKEND). pytest steps use the venv python; lint-imports uses its console script.
STEPS: tuple[tuple[str, list[str]], ...] = (
    ("egress-suite", [PY, "-m", "pytest", "tests/core/test_net_safety.py", "-q"]),
    ("star-suite", [PY, "-m", "pytest", "tests/governance/test_p5_app_star_boundary.py", "-q"]),
    ("mcp-redteam", [PY, "-m", "pytest", "tests/mcp", "-q", "-k", "redteam or isolation or leak"]),
    ("portal-redteam", [PY, "-m", "pytest", "tests/portal_copilot", "-q", "-k", "leak or redteam"]),
    ("public-api-isolation", [PY, "-m", "pytest", "tests/public_api", "-q", "-k", "isolation or cross_tenant"]),
    ("conformance", [PY, "-m", "pytest", "tests/marketplace", "-q", "-k", "conformance"]),
    ("secret-scan", [PY, "-m", "pytest", "tests", "-q", "-k", "log_scan or secret_scan or no_secret"]),
    ("lint-imports", [str(BACKEND / "venv" / "Scripts" / "lint-imports")]),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="list the gate steps and exit 0 (no keys needed)")
    args = parser.parse_args()
    if args.dry_run:
        for name, cmd in STEPS:
            print(f"[dry-run] {name}: {' '.join(cmd)}")
        return 0
    failures: list[str] = []
    for name, cmd in STEPS:
        print(f"==== p5-gate: {name} ====", flush=True)
        proc = subprocess.run(cmd, cwd=str(BACKEND), check=False)
        if proc.returncode != 0:
            failures.append(name)
    if failures:
        print(f"p5-gate FAILED: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("p5-gate: all steps passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
