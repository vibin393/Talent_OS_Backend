# ruff: noqa: E501, D401
"""Runtime beat jobs per tenant (Epic 53, AD-58): reaper, proposal expiry, budget alerts, payload purge.

Epic 66 (§0.15): ``origin='external'`` (MCP) sessions hold no lease and are never touched by ``reap``;
``close_idle_external`` (beat ``agents.close_idle_external``, 5 min) stops sessions idle > 30 minutes."""

import uuid
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic.agents import agents
from app.core.events import emit
from app.core.settings import get_settings
from app.events.agents import AgentBudgetThresholdCrossed
from app.models.agent_runtime import AgentConfig, AgentRun
from app.services.agent_runtime import payloads, proposals, state
from app.services.agent_runtime.executor import finalize
from app.services.agent_runtime.runs import request_step

QUEUED_STALE_SECONDS = 120
_sent_local: set[str] = set()


def _now() -> datetime:
    return datetime.now(UTC)


async def reap(db: AsyncSession) -> dict[str, int]:
    now = _now()
    counts = {"lease": 0, "wait": 0, "deadline": 0, "queued": 0}
    for run in (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.status == "running", AgentRun.lease_expires_at < now)
            .with_for_update(skip_locked=True)
        )
    ).scalars():
        run.lease_owner, run.lease_expires_at = None, None
        request_step(db, run, run.next_step_no)
        counts["lease"] += 1
    for run in (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.status == "waiting", AgentRun.wait_until < now)
            .with_for_update(skip_locked=True)
        )
    ).scalars():
        await finalize(db, run, "stopped", "timeout")
        counts["wait"] += 1
    for run in (
        await db.execute(
            select(AgentRun)
            .where(
                AgentRun.status.in_(("queued", "running", "waiting")),
                AgentRun.deadline_at < now,
                AgentRun.origin != "external",
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars():
        await finalize(db, run, "stopped", "timeout")
        counts["deadline"] += 1
    g = await state.gates(db)
    if g.provider_down_since is None and not g.any_kill:
        for run in (
            await db.execute(
                select(AgentRun)
                .where(
                    AgentRun.status == "queued",
                    AgentRun.lease_owner.is_(None),
                    AgentRun.created_at < now - timedelta(seconds=QUEUED_STALE_SECONDS),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars():
            request_step(db, run, run.next_step_no)
            counts["queued"] += 1
    await db.flush()
    return counts


async def expire_proposals(db: AsyncSession) -> dict[str, int]:
    from app.services import notifications_service as ns

    soon = await proposals.expiring_soon(db)
    for p in soon:
        recipients = [p.assignee_user_id] if p.assignee_user_id else []
        if recipients:
            content = ns.Content(
                title=f"AI proposal {p.human_id} expires within 24 hours"[:300],
                entity_type="agent_proposal",
                entity_id=p.id,
                link_path=f"/ai/proposals/{p.id}",
            )
            await ns.deliver(db, "proposal_expiring", recipients, content, event_id=f"expiring:{p.id}")
    expired = await proposals.expire_due(db)
    return {"expiring": len(soon), "expired": expired}


async def _mark_once(key: str) -> bool:
    """True the first time ``key`` is seen this month (Redis SET NX; in-process fallback)."""
    s = get_settings()
    if s.celery_task_always_eager or s.rate_limit_backend != "redis":
        if key in _sent_local:
            return False
        _sent_local.add(key)
        return True
    try:
        from redis.asyncio import Redis

        client = Redis(
            host=s.redis_host, port=s.redis_port, db=2, password=s.redis_password or None, socket_timeout=0.5
        )
        try:
            return bool(await client.set(key, "1", nx=True, ex=40 * 24 * 3600))
        finally:
            await client.aclose()
    except Exception:
        logger.warning("Agent budget alerts: Redis unavailable; using in-process markers")
        if key in _sent_local:
            return False
        _sent_local.add(key)
        return True


async def budget_alerts(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    period = _now().strftime("%Y-%m")
    sent = 0
    configs = (
        await db.execute(select(AgentConfig).where(AgentConfig.monthly_token_budget.is_not(None)))
    ).scalars()
    for config in configs:
        if config.agent_key not in agents() or not config.monthly_token_budget:
            continue
        used = await state.agent_tokens_this_month(db, config.agent_key)
        for threshold in (80, 100):
            if used * 100 >= threshold * config.monthly_token_budget and await _mark_once(
                f"agents:budget:{tenant_id}:{config.agent_key}:{period}:{threshold}"
            ):
                emit(db, AgentBudgetThresholdCrossed(
                    tenant_id=tenant_id, agent_key=config.agent_key, period=period, threshold=threshold, used=used,
                    budget=int(config.monthly_token_budget),
                ))  # fmt: skip
                sent += 1
    return sent


async def purge_payloads(db: AsyncSession) -> int:
    return await payloads.purge_expired(db)


async def close_idle_external(db: AsyncSession) -> int:
    from app.services.agent_runtime.external import close_idle_external as close

    return await close(db)


__all__ = ["budget_alerts", "close_idle_external", "expire_proposals", "purge_payloads", "reap"]
