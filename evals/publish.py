# ruff: noqa: E501
"""Build the ``EvalReport`` (Epic 65 story 65.5) from per-case results.

``EvalReport = {run: {mode, trigger, git_sha, models, started_at, finished_at, status},
results: [{agent_key, agent_version, suite, prompt_ref, model, fingerprint, cases_total, cases_passed, score,
threshold, passed, failures}]}``. ``failures`` holds ``{case_id, assertion, code}`` only -- never case inputs or
outputs (the assertion is the name of the failed check, e.g. ``status``, ``proposals``, ``tool``).
"""

import dataclasses
import json
import subprocess
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from evals.gate import REDTEAM_THRESHOLD, fingerprint, model_ids, prompt_ref, threshold_for

SUITE_AGENT, SUITE_REDTEAM = "agent", "redteam"


@dataclasses.dataclass(frozen=True, slots=True)
class CaseResult:
    suite_dir: str  # evals/<dir> (``redteam`` -> red-team suite)
    case_id: str
    agent: str
    passed: bool
    problems: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    model: str | None = None

    @property
    def suite(self) -> str:
        return SUITE_REDTEAM if self.suite_dir == "redteam" else SUITE_AGENT

    def to_json(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "problems": list(self.problems)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "CaseResult":
        return cls(**{**data, "problems": tuple(data.get("problems") or ())})


def _assertion(problem: str) -> str:
    head = (problem or "").strip().split(" ", 1)[0].strip(":'\"")
    return head[:40] or "check"


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha[:40] if out.returncode == 0 and sha else None


def sdk_version() -> str | None:
    try:
        import anthropic

        return str(anthropic.__version__)[:20]
    except ImportError:  # pragma: no cover
        return None


def build_report(
    results: Iterable[CaseResult],
    *,
    mode: str,
    trigger: str,
    started_at: datetime,
    finished_at: datetime,
    gate_enforced: bool = True,
    status: str | None = None,
) -> dict[str, Any]:
    from app.core.agentic import agents

    registry = agents()
    groups: dict[tuple[str, str], list[CaseResult]] = {}
    for r in results:
        groups.setdefault((r.agent, r.suite), []).append(r)
    models = model_ids()
    out: list[dict[str, Any]] = []
    for (agent, suite), cases in sorted(groups.items()):
        total = len(cases)
        passed_n = sum(1 for c in cases if c.passed)
        score = (
            (Decimal(passed_n) / Decimal(total)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            if total
            else Decimal(0)
        )
        threshold = REDTEAM_THRESHOLD if suite == SUITE_REDTEAM else threshold_for(agent)
        spec = registry.get(agent)
        failures = [
            {"case_id": c.case_id, "assertion": _assertion(c.problems[0] if c.problems else ""), "code": "failed"}
            for c in cases if not c.passed
        ]  # fmt: skip
        model_seen = next((c.model for c in cases if c.model), None)
        out.append({
            "agent_key": agent, "agent_version": int(getattr(spec, "version", 1) or 1), "suite": suite,
            "prompt_ref": prompt_ref(agent), "model": model_seen or (models["standard"] if mode == "live" else "fake"),
            "fingerprint": fingerprint(agent), "cases_total": total, "cases_passed": passed_n, "score": str(score),
            "threshold": str(threshold), "passed": score >= threshold, "failures": failures,
            "input_tokens": sum(c.input_tokens for c in cases), "output_tokens": sum(c.output_tokens for c in cases),
            "cost_micros": sum(c.cost_micros for c in cases),
        })  # fmt: skip
    ok = all(r["passed"] for r in out)
    run_models: dict[str, Any] = dict(models)
    if not gate_enforced:
        run_models["_gate_enforced"] = "false"
    return {
        "run": {
            "mode": mode, "trigger": trigger, "git_sha": git_sha(), "sdk_version": sdk_version(), "models": run_models,
            "started_at": started_at.isoformat(), "finished_at": finished_at.isoformat(),
            "status": status or ("passed" if ok else "failed"),
        },
        "results": out,
    }  # fmt: skip


def write_json(report: Mapping[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def gate_failures(report: Mapping[str, Any]) -> list[str]:
    """Human-readable gate failures: agents below threshold and failing case ids."""
    lines: list[str] = []
    for r in report["results"]:
        if r["passed"]:
            continue
        ids = ", ".join(f["case_id"] for f in r["failures"]) or "-"
        kind = "red-team failure" if r["suite"] == SUITE_REDTEAM else "below threshold"
        lines.append(
            f"{r['agent_key']} [{r['suite']}] {kind}: score {r['score']} < {r['threshold']}; cases: {ids}"
        )
    return lines


__all__ = ["CaseResult", "build_report", "gate_failures", "git_sha", "write_json"]
