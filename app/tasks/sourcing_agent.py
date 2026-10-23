# ruff: file-ignore[line-too-long]
"""Sourcing Agent tasks (Epic 56). Every task starts with ``tenant_id`` (the beat fan-out excepted). Queue
``maintenance``.

* ``sourcing_agent.on_match_run`` -- a completed P1 match run: re-rank trigger for ``requirements_changed`` runs.
* ``sourcing_agent.cancel_for_demand`` -- the Demand left ``open``: cancel active sourcing runs (``subject_closed``).
* ``sourcing_agent.on_proposal_decided`` -- an ``outreach.send`` Proposal was decided: the entry's outreach status.
* ``sourcing_agent.weekly_refresh`` (beat, Mondays 03:30 UTC) -- the ``weekly_refresh`` trigger for Demands open for
  more than 14 days without a run in 7 days, and deletion of superseded shortlists older than 365 days.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"
SUPERSEDED_RETENTION_DAYS = 365


@celery_app.task(name="sourcing_agent.on_match_run", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_match_run(tenant_id: str, demand_id: str, match_run_id: str, status: str) -> bool:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> bool:
        from app.services.sourcing_agent import triggers

        async with tenant_session(tid) as db:
            return await triggers.on_match_run(db, uuid.UUID(demand_id), uuid.UUID(match_run_id), status)

    return bool(run(job, tenant_id=tid))


@celery_app.task(name="sourcing_agent.cancel_for_demand", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def cancel_for_demand(tenant_id: str, demand_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.sourcing_agent import triggers

        async with tenant_session(tid) as db:
            return await triggers.cancel_for_demand(db, tid, uuid.UUID(demand_id))

    return int(run(job, tenant_id=tid))


@celery_app.task(name="sourcing_agent.on_proposal_decided", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_proposal_decided(tenant_id: str, proposal_id: str, status: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.sourcing_agent import triggers

        async with tenant_session(tid) as db:
            return await triggers.on_proposal_decided(db, uuid.UUID(proposal_id), status)

    return int(run(job, tenant_id=tid))


async def purge_superseded(db: Any, now: datetime) -> int:
    from sqlalchemy import delete

    from app.models.sourcing_agent import AgentShortlist

    result = await db.execute(
        delete(AgentShortlist).where(
            AgentShortlist.status == "superseded",
            AgentShortlist.created_at < now - timedelta(days=SUPERSEDED_RETENTION_DAYS),
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


@celery_app.task(name="sourcing_agent.weekly_refresh")  # type: ignore[misc,untyped-decorator,unused-ignore]
def weekly_refresh() -> dict[str, Any]:
    now = datetime.now(UTC)

    async def job(tid: uuid.UUID) -> dict[str, int]:
        from app.services.sourcing_agent import triggers

        async with tenant_session(tid) as db:
            started = await triggers.weekly_refresh(db, tid, now)
            purged = await purge_superseded(db, now)
            return {"refresh": started, "purged": purged}

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


__all__ = ["cancel_for_demand", "on_match_run", "on_proposal_decided", "purge_superseded", "weekly_refresh"]
