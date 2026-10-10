# ruff: noqa: E501
"""Eval runner (AD-69; Epic 53 offline harness, Epic 65 CLI / live mode / store / gate, FR-322).

Cases run inside pytest (``tests/agents/test_evals_offline.py``, marker ``evals``) against a seeded test tenant. In
offline mode the Fake LLM plays the model -- including a compromised one; in live mode (``EVALS_LIVE=1``) the
configured real model answers at temperature 0 and rubric items are judged by the ``fast`` tier.

    python -m evals.run --list
    python -m evals.run --mode offline --gate [--agent KEY ...] [--suite agents|redteam|all] [--json PATH]
    python -m evals.run --mode live --record --trigger schedule      # needs ANTHROPIC_API_KEY_LIVE + EVAL_DATABASE_URL
    python -m evals.run --mode live --dry-run                         # "skipped: no live key" without a key (exit 0)
"""

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.assertions import check
from evals.fixtures import build, fill
from evals.schema import EvalCase, load_cases

ROOT = Path(__file__).resolve().parent
SUITES = ("foundation", "redteam")
# Epic 73: protocol-level suites (scripted MCP client, not agent runs) -- run by tests/mcp/test_redteam.py (-m evals).
MCP_SUITES: dict[str, str] = {"mcp_redteam": "tests/mcp/test_redteam.py"}
LAST_METRICS: dict[str, dict[str, Any]] = {}  # case id -> tokens / cost of its last run (report only)
TEST_FILE = "tests/agents/test_evals_offline.py"


def all_cases() -> list[tuple[str, EvalCase]]:
    out: list[tuple[str, EvalCase]] = []
    for suite_dir in sorted(
        p for p in ROOT.iterdir() if p.is_dir() and p.name != "fake_scripts" and p.name not in MCP_SUITES and not p.name.startswith("_")
    ):
        out.extend((suite_dir.name, case) for case in load_cases(suite_dir))
    return out


async def run_case(case: EvalCase, org: Any) -> list[str]:
    """Run one case against ``org`` (tenant + owner + candidate) offline; returns the governance problems."""
    from sqlalchemy import select

    from app.core.agentic import Kickoff
    from app.core.database import tenant_session
    from app.models.activity import Activity
    from app.models.agent_runtime import AgentProposal, AgentRun, AgentToolCall
    from app.services import agent_runtime_service as svc
    from app.services.ai.fake_llm import FakeTurn, fake_llm

    values = await build(case.seed, org)
    turns = [FakeTurn.from_mapping(fill(t, values)) for t in case.script]
    subject = ("candidate", org.candidate_id) if case.subject == "candidate" else None
    if case.subject == "seed":  # Epic 56: the seed builder names the subject (e.g. a Demand)
        import uuid

        subject = (values["subject_type"], uuid.UUID(values["subject_id"]))
    subject_ids = {
        str(org.candidate_id),
        *(i for i in values.get("subject_candidate_ids", "").split(",") if i),
    }
    kickoff = (
        Kickoff(inputs={"case": case.id}, untrusted=dict(case.kickoff_untrusted))
        if case.kickoff_untrusted
        else None
    )
    if not case.agent.startswith("_"):  # Epic 54: owner agents are gated by their own feature ``agent_<key>``
        from app.services import ai

        async with tenant_session(org.tenant_id) as db:
            await ai.set_features(db, org.tenant_id, {"agentic": True, f"agent_{case.agent}": True})
    live = os.environ.get("EVALS_LIVE") == "1"
    if live:  # Epic 65: the real model answers (key from ANTHROPIC_API_KEY_LIVE only)
        from app.core.settings import get_settings
        from evals.live import live_key

        get_settings().anthropic_api_key = live_key()
    with contextlib.nullcontext() if live else fake_llm(turns):
        async with tenant_session(org.tenant_id) as db:
            ref = await svc.start_run(
                org.ctx(), db, agent_key=case.agent, trigger_kind="manual", subject=subject, kickoff=kickoff,
                owner_user_id=org.admin.user_id,
            )  # fmt: skip
    if ref.run_id is None:
        return [f"run not started: {ref.reason}"]
    async with tenant_session(org.tenant_id) as db:
        run = await db.get(AgentRun, ref.run_id)
        calls = list(
            (
                await db.execute(
                    select(AgentToolCall)
                    .where(AgentToolCall.run_id == ref.run_id)
                    .order_by(AgentToolCall.step_no, AgentToolCall.created_at)
                )
            ).scalars()
        )
        proposals = list(
            (await db.execute(select(AgentProposal).where(AgentProposal.run_id == ref.run_id))).scalars()
        )
        bodies = list(
            (
                await db.execute(
                    select(Activity.body).where(
                        Activity.entity_type == "candidate", Activity.entity_id == org.candidate_id
                    )
                )
            ).scalars()
        )
        problems = check(case.expect, run, calls, proposals, bodies, subject_ids=subject_ids)
        LAST_METRICS[case.id] = {
            "input_tokens": int(run.input_tokens or 0), "output_tokens": int(run.output_tokens or 0),
            "cost_micros": int(run.cost_micros or 0),
        }  # fmt: skip
        if live and case.rubric:
            from evals.live import judge

            material = {
                "status": run.status, "reason_code": run.reason_code, "result": run.result,
                "tool_calls": [{"tool": c.tool_key, "outcome": c.outcome, "mode": c.mode} for c in calls],
                "proposals": [p.tool_key for p in proposals],
            }  # fmt: skip
            score, model = await judge(case.rubric, material)
            LAST_METRICS[case.id]["model"] = model
            if score != 1:
                problems.append("rubric not satisfied")
        return problems


def record_result(suite: str, case: EvalCase, problems: list[str]) -> None:
    """Append one case result to ``EVALS_RESULTS_PATH`` (JSON lines) when the CLI drives pytest."""
    path = os.environ.get("EVALS_RESULTS_PATH")
    if not path:
        return
    from evals.publish import CaseResult

    metrics = LAST_METRICS.get(case.id, {})
    row = CaseResult(
        suite_dir=suite, case_id=case.id, agent=case.agent, passed=not problems, problems=tuple(problems),
        input_tokens=int(metrics.get("input_tokens", 0)), output_tokens=int(metrics.get("output_tokens", 0)),
        cost_micros=int(metrics.get("cost_micros", 0)), model=metrics.get("model"),
    )  # fmt: skip
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row.to_json()) + "\n")


def selected(suite: str, case: EvalCase) -> bool:
    """Case filter of a CLI-driven pytest run (``EVALS_AGENTS`` comma list, ``EVALS_SUITE``)."""
    agents = [a for a in os.environ.get("EVALS_AGENTS", "").split(",") if a]
    wanted = os.environ.get("EVALS_SUITE", "all")
    if agents and case.agent not in agents:
        return False
    if wanted == "redteam" and suite != "redteam":
        return False
    return not (wanted == "agents" and suite == "redteam")


def execute(mode: str, agents: list[str], suite: str, *, eval_db: str | None = None) -> list[Any]:
    """Run the cases under pytest (seeded test tenant) and return the per-case results."""
    from evals.publish import CaseResult

    backend = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="evals_") as tmp:
        out = Path(tmp) / "results.jsonl"
        env = {
            **os.environ,
            "EVALS_RESULTS_PATH": str(out),
            "EVALS_AGENTS": ",".join(agents),
            "EVALS_SUITE": suite,
        }
        if mode == "live":
            from sqlalchemy.engine import make_url

            env["EVALS_LIVE"] = "1"
            env["TEST_DB_NAME"] = str(make_url(eval_db or "").database)
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "evals",
            "-q",
            "-p",
            "no:cacheprovider",
            TEST_FILE,
            "-k",
            "test_case",
        ]
        subprocess.run(cmd, cwd=backend, env=env, check=False)
        if not out.exists():
            return []
        lines = out.read_text(encoding="utf-8").splitlines()
        return [CaseResult.from_json(json.loads(line)) for line in lines if line]


def main(argv: list[str] | None = None) -> int:
    from app.core.settings import get_settings
    from evals import live as live_mod
    from evals.gate import eval_agents
    from evals.publish import build_report, gate_failures, write_json

    parser = argparse.ArgumentParser(prog="evals.run")
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--agent", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--suite", choices=("agents", "redteam", "all"), default="all")
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--trigger", choices=("ci", "schedule", "manual", "release"), default="manual")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        for suite, case in all_cases():
            if args.agent and case.agent not in args.agent:
                continue
            print(f"{suite}/{case.id}: {case.description}")
        return 0
    eval_db = None
    if args.mode == "live":
        if not live_mod.live_key():
            if args.dry_run:
                print("skipped: no live key")
                return 0
            print("live mode needs ANTHROPIC_API_KEY_LIVE")
            return 2
        try:
            live_mod.check_target(get_settings().eval_database_url)
        except live_mod.UnsafeTarget as exc:
            print(f"refused: {exc}")
            return 2
        eval_db = get_settings().eval_database_url
    agents = [] if args.all else list(args.agent)
    if agents:
        known = set(eval_agents()) | {"_test_agent", "_test_conversational"}
        unknown = sorted(set(agents) - known)
        if unknown:
            print(f"unknown agent(s): {', '.join(unknown)}")
            return 2
    started = datetime.now(UTC)
    results = execute(args.mode, agents, args.suite, eval_db=eval_db)
    finished = datetime.now(UTC)
    enforced = bool(get_settings().eval_gate_enforced)
    report = build_report(
        results, mode=args.mode, trigger=args.trigger, started_at=started, finished_at=finished,
        gate_enforced=enforced, status="error" if not results else None,
    )  # fmt: skip
    if args.json_path:
        write_json(report, args.json_path)
    if args.record:
        from evals import store

        run_id = asyncio.run(store.record(report))
        print(f"recorded eval run {run_id}")
    passed = sum(1 for r in results if r.passed)
    print(f"{passed}/{len(results)} cases passed ({args.mode})")
    if args.gate:
        if not results:
            print("GATE FAILED: no case ran")
            return 1
        failures = gate_failures(report)
        if failures:
            for line in failures:
                print(f"GATE FAILED: {line}")
            if not enforced:
                print("gate not enforced (EVAL_GATE_ENFORCED=false); recorded in the run")
                return 0
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
