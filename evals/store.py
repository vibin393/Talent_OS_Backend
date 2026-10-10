# ruff: noqa: E501
"""Evaluation results store (Epic 65 story 65.5): writes ``ai_eval_runs`` / ``ai_eval_results`` (platform tables) to
``EVAL_RESULTS_DATABASE_URL`` (default: the app database as ``talent_app``) and reads live results for the release
gate. Only aggregate scores and failing case ids are stored -- never case inputs or outputs."""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool


def results_url() -> str:
    from app.core.settings import get_settings

    s = get_settings()
    return s.eval_results_database_url or s.app_db_url()


def _engine(url: str | None = None) -> AsyncEngine:
    return create_async_engine(url or results_url(), poolclass=NullPool)


def _to_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


async def record(
    report: Mapping[str, Any], *, url: str | None = None, engine: AsyncEngine | None = None
) -> uuid.UUID:
    """Insert one run and its per-(agent, suite) results; returns the run id."""
    from app.models.ai_evals import AIEvalResult, AIEvalRun

    run = report["run"]
    run_id = uuid.uuid4()
    eng = engine or _engine(url)
    try:
        async with eng.begin() as conn:
            await conn.execute(insert(AIEvalRun).values(
                id=run_id, mode=run["mode"], trigger=run["trigger"], git_sha=run.get("git_sha"),
                sdk_version=run.get("sdk_version"), models=dict(run.get("models") or {}), status=run["status"],
                started_at=_to_dt(run.get("started_at")), finished_at=_to_dt(run.get("finished_at")),
            ))  # fmt: skip
            for r in report["results"]:
                await conn.execute(insert(AIEvalResult).values(
                    id=uuid.uuid4(), run_id=run_id, agent_key=r["agent_key"], agent_version=int(r["agent_version"]),
                    suite=r["suite"], prompt_ref=r.get("prompt_ref"), model=r.get("model"), fingerprint=r["fingerprint"],
                    cases_total=int(r["cases_total"]), cases_passed=int(r["cases_passed"]),
                    score=Decimal(str(r["score"])), threshold=Decimal(str(r["threshold"])), passed=bool(r["passed"]),
                    failures=[{"case_id": f["case_id"], "assertion": f.get("assertion"), "code": f.get("code")} for f in r.get("failures") or []],
                    input_tokens=int(r.get("input_tokens") or 0), output_tokens=int(r.get("output_tokens") or 0),
                    cost_micros=int(r.get("cost_micros") or 0),
                ))  # fmt: skip
    finally:
        if engine is None:
            await eng.dispose()
    return run_id


async def live_results(
    agent_keys: Sequence[str], *, url: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Live results per agent (newest first) for the release gate."""
    from app.models.ai_evals import AIEvalResult, AIEvalRun

    eng = _engine(url)
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in agent_keys}
    try:
        async with eng.connect() as conn:
            rows = await conn.execute(
                select(AIEvalResult.agent_key, AIEvalResult.suite, AIEvalResult.fingerprint, AIEvalResult.passed,
                       AIEvalResult.created_at)
                .join(AIEvalRun, AIEvalRun.id == AIEvalResult.run_id)
                .where(AIEvalRun.mode == "live", AIEvalResult.agent_key.in_(list(agent_keys)))
                .order_by(AIEvalResult.created_at.desc())
            )  # fmt: skip
            for key, suite, fp, passed, created in rows.tuples():
                out.setdefault(key, []).append(
                    {
                        "suite": suite,
                        "fingerprint": fp,
                        "passed": passed,
                        "created_at": created or datetime.now(UTC),
                    }
                )
    finally:
        await eng.dispose()
    return out


__all__ = ["live_results", "record", "results_url"]
