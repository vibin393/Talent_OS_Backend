# ruff: file-ignore[line-too-long]
"""Evaluation results for tenants and regression notices (Epic 65 story 65.5, FR-322).

* ``latest_for_tenant`` -- per registered non-placeholder agent with an evaluation set: the latest offline and live
  agent-suite scores, the latest red-team counts and the status (``passing`` / ``below_threshold`` / ``not_run``).
  Platform data only (``ai_eval_results``); no case details beyond failing case ids.
* ``regressions`` / ``notify_regressions`` -- after a recorded live run, agents that dropped below threshold or failed a
  red-team case compared with their previous live result; tenants with ``agent_<key>`` on get ``ai_eval_regression``.
"""

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic.agents import agents
from app.core.context import RequestContext
from app.models.ai_evals import AIEvalResult, AIEvalRun
from app.schemas.ai_governance import EvalRowOut, EvalScore, RedteamScore

DEFAULT_THRESHOLD = Decimal("0.90")


def _eval_agents() -> dict[str, Any]:
    return {
        k: s
        for k, s in sorted(agents().items())
        if not k.startswith("_") and not s.placeholder and s.eval_set
    }


def _threshold(spec: Any) -> Decimal:
    return max(
        Decimal(str(getattr(spec, "eval_threshold", None) or DEFAULT_THRESHOLD)), DEFAULT_THRESHOLD
    ).quantize(Decimal("0.01"))


async def _latest(db: AsyncSession, keys: Sequence[str]) -> dict[tuple[str, str, str], Any]:
    """(agent, suite, mode) -> newest result row."""
    rows = await db.execute(
        select(AIEvalResult, AIEvalRun.mode)
        .join(AIEvalRun, AIEvalRun.id == AIEvalResult.run_id)
        .where(AIEvalResult.agent_key.in_(list(keys)))
        .order_by(AIEvalResult.created_at.desc())
        .limit(2000)
    )  # fmt: skip
    out: dict[tuple[str, str, str], Any] = {}
    for result, mode in rows.tuples():
        out.setdefault((result.agent_key, result.suite, mode), result)
    return out


def _score(r: Any) -> EvalScore | None:
    if r is None:
        return None
    return EvalScore(score=str(r.score), threshold=str(r.threshold), passed=bool(r.passed), created_at=r.created_at,
                     model=r.model, prompt_ref=r.prompt_ref)  # fmt: skip


def _row(key: str, spec: Any, latest: dict[tuple[str, str, str], Any]) -> EvalRowOut:
    offline = latest.get((key, "agent", "offline"))
    live = latest.get((key, "agent", "live"))
    red = latest.get((key, "redteam", "live")) or latest.get((key, "redteam", "offline"))
    newest = live or offline
    if newest is None:
        status = "not_run"
    elif newest.passed and (red is None or red.passed):
        status = "passing"
    else:
        status = "below_threshold"
    failing = [
        f.get("case_id")
        for r in (newest, red)
        if r is not None
        for f in (r.failures or [])
        if f.get("case_id")
    ]
    times = [r.created_at for r in (offline, live, red) if r is not None]
    return EvalRowOut(
        agent_key=key, agent_name=spec.name, agent_version=spec.version,
        prompt_ref=newest.prompt_ref if newest is not None else None, model=newest.model if newest is not None else None,
        offline=_score(offline), live=_score(live),
        redteam=RedteamScore(cases_total=red.cases_total, cases_passed=red.cases_passed) if red is not None else None,
        threshold=str(_threshold(spec)), status=status, failing_case_ids=sorted(set(failing))[:50],  # type: ignore[arg-type]
        last_run_at=max(times) if times else None,
    )  # fmt: skip


async def latest_for_tenant(ctx: RequestContext, db: AsyncSession) -> list[EvalRowOut]:  # ruff: ignore[unused-function-argument]
    specs = _eval_agents()
    latest = await _latest(db, list(specs))
    return [_row(k, s, latest) for k, s in specs.items()]


async def latest_for_agent(db: AsyncSession, agent_key: str) -> EvalRowOut | None:
    spec = _eval_agents().get(agent_key)
    if spec is None:
        return None
    return _row(agent_key, spec, await _latest(db, [agent_key]))


# ---------------------------------------------------------------- regressions
async def regressions(db: AsyncSession, run_id: uuid.UUID) -> list[str]:
    """Agents of live run ``run_id`` that regressed: below threshold or a red-team failure, where the previous live
    result of the same (agent, suite) passed (or did not exist)."""
    rows = list((await db.execute(select(AIEvalResult).where(AIEvalResult.run_id == run_id))).scalars())
    out: set[str] = set()
    for r in rows:
        if r.passed:
            continue
        prev = (
            await db.execute(
                select(AIEvalResult.passed)
                .join(AIEvalRun, AIEvalRun.id == AIEvalResult.run_id)
                .where(AIEvalRun.mode == "live", AIEvalResult.agent_key == r.agent_key, AIEvalResult.suite == r.suite,
                       AIEvalResult.run_id != run_id, AIEvalResult.created_at < r.created_at)
                .order_by(AIEvalResult.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()  # fmt: skip
        if prev is None or prev:
            out.add(r.agent_key)
    return sorted(out)


__all__ = ["latest_for_agent", "latest_for_tenant", "regressions"]
