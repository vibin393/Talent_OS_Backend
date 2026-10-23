# ruff: file-ignore[line-too-long]
"""Deterministic run starters (Epic 62 A3): hourly beats on ``maintenance`` that act at the tenant's local run hour.

* ``compliance_tick`` -- one Compliance Agent run per Worker with an item due today (dedup ``daily:{candidate}:{date}``).
* ``bgv_tick`` -- one BGV Agent run per case with a provider follow-up due today (dedup ``daily:{case}:{date}``).
No model call happens when nothing is due.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services import entity_refs
from app.services.chasing import bgv_follow_ups, chases

RUN_HOUR_LOCAL = 9


async def _local(db: AsyncSession) -> tuple[date, int]:
    tenant = await entity_refs.tenant(db, entity_refs.session_tenant(db))
    now = datetime.now(tenant.tz)
    return now.date(), now.hour


async def compliance_tick(
    db: AsyncSession, *, force: bool = False, today: date | None = None
) -> dict[str, Any]:
    from app.services import agent_runtime_service as runtime

    day, hour = await _local(db)
    day = today or day
    if not force and hour != RUN_HOUR_LOCAL:
        return {"skipped": "not_run_hour"}
    tid = entity_refs.session_tenant(db)
    started: list[str] = []
    for cid in await chases.due_candidates(db, day):
        ref = await runtime.start_run(
            RequestContext(tenant_id=tid, actor_kind="system"), db, agent_key="compliance", trigger_kind="schedule",
            trigger_key="daily_scan", subject=("candidate", cid), dedup_key=f"daily:{cid}:{day.isoformat()}",
        )  # fmt: skip
        started.append(str(ref.run_id) if ref.run_id else f"not_started:{ref.reason}")
    return {"runs": started}


async def start_for_candidate(db: AsyncSession, candidate_id: uuid.UUID, trigger_key: str) -> str | None:
    """``placement_created``: a run when something is already due for the new Worker (start within 14 days)."""
    from app.services import agent_runtime_service as runtime

    day, _ = await _local(db)
    r = await chases.compute(db, candidate_id, day)
    if not r.plan.items:
        return None
    ref = await runtime.start_run(
        RequestContext(tenant_id=entity_refs.session_tenant(db), actor_kind="system"), db, agent_key="compliance",
        trigger_kind="event", trigger_key=trigger_key, subject=("candidate", candidate_id),
        dedup_key=f"daily:{candidate_id}:{day.isoformat()}",
    )  # fmt: skip
    return str(ref.run_id) if ref.run_id else ref.reason


async def bgv_tick(db: AsyncSession, *, force: bool = False, today: date | None = None) -> dict[str, Any]:
    from app.services import agent_runtime_service as runtime

    day, hour = await _local(db)
    day = today or day
    if not force and hour != RUN_HOUR_LOCAL:
        return {"skipped": "not_run_hour"}
    tid = entity_refs.session_tenant(db)
    started: list[str] = []
    for case_id in await bgv_follow_ups.cases_with_follow_up_due(db, day):
        ref = await runtime.start_run(
            RequestContext(tenant_id=tid, actor_kind="system"), db, agent_key="bgv", trigger_kind="schedule",
            trigger_key="daily_follow_up", subject=("bgv_case", case_id), dedup_key=f"daily:{case_id}:{day.isoformat()}",
        )  # fmt: skip
        started.append(str(ref.run_id) if ref.run_id else f"not_started:{ref.reason}")
    return {"runs": started}


__all__ = ["RUN_HOUR_LOCAL", "bgv_tick", "compliance_tick", "start_for_candidate"]
