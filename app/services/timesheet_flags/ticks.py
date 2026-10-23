# ruff: file-ignore[line-too-long]
"""Timesheet Agent run starters (Epic 63 A2). Beat ``timesheet_flags.nudge_tick`` (hourly, queue ``maintenance``) acts
at 10:00 tenant-local time: one ``timesheet`` run per Account Manager with a non-empty nudge plan
(``trigger_key="daily_nudges"``, subject ``("user", am)``, owner = the AM, dedup ``nudges:{am}:{date}``). No model call
happens when nothing is due; with the agent off ``start_run`` returns ``feature_off`` and P2 behaves as today.

``start_flag_run`` -- the ``flags_raised`` trigger: one run per evaluated timesheet version (subject ``timesheet``,
owner = the Placement AM, dedup ``flags:{timesheet}:{version}``) to write the explanations.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic import Kickoff
from app.core.context import RequestContext
from app.services import entity_refs
from app.services.timesheet_flags import actions, nudges

RUN_HOUR_LOCAL = 10
AGENT_KEY = "timesheet"


async def _local(db: AsyncSession) -> tuple[date, int, str]:
    tenant = await entity_refs.tenant(db, entity_refs.session_tenant(db))
    now = datetime.now(tenant.tz)
    return now.date(), now.hour, str(tenant.tz)


def nudge_kickoff(am_id: uuid.UUID, today: date, timezone: str) -> Kickoff:
    return Kickoff(
        inputs={
            "run_kind": "nudge", "account_manager_id": str(am_id), "today": today.isoformat(), "timezone": timezone,
        }
    )  # fmt: skip


async def start_nudge_run(
    ctx: RequestContext, db: AsyncSession, am_id: uuid.UUID, *, trigger_key: str = "daily_nudges", trigger_kind: str = "schedule"
) -> Any:
    from app.services import agent_runtime_service as runtime

    today, _, tz = await _local(db)
    return await runtime.start_run(
        ctx, db, agent_key=AGENT_KEY, trigger_kind=trigger_kind, trigger_key=trigger_key, subject=("user", am_id),
        owner_user_id=am_id, kickoff=nudge_kickoff(am_id, today, tz),
        dedup_key=f"nudges:{am_id}:{today.isoformat()}" if trigger_kind == "schedule" else None,
    )  # fmt: skip


async def nudge_tick(db: AsyncSession, *, force: bool = False, today: date | None = None) -> dict[str, Any]:
    day, hour, _ = await _local(db)
    day = today or day
    if not force and hour != RUN_HOUR_LOCAL:
        return {"skipped": "not_run_hour"}
    tid = entity_refs.session_tenant(db)
    ctx = RequestContext(tenant_id=tid, actor_kind="system")
    started: list[str] = []
    for am in await nudges.account_managers_with_items(db):
        plan = await nudges.plan_for(db, am, day, datetime.now(UTC))
        for item in plan.max_reached:  # after the last nudge: a Task for the AM (no model call needed)
            await actions.max_reached_task(ctx, db, am, item)
        if not (plan.submit or plan.approve or plan.escalate):
            continue
        ref = await start_nudge_run(ctx, db, am)
        started.append(str(ref.run_id) if ref.run_id else f"not_started:{ref.reason}")
    return {"runs": started}


async def start_flag_run(db: AsyncSession, timesheet_id: uuid.UUID, version: int) -> str | None:
    from app.services import agent_runtime_service as runtime

    am = await actions.am_of(db, timesheet_id)
    if am is None:
        return None
    ref = await runtime.start_run(
        RequestContext(tenant_id=entity_refs.session_tenant(db), actor_kind="system"), db, agent_key=AGENT_KEY,
        trigger_kind="event", trigger_key="flags_raised", subject=("timesheet", timesheet_id), owner_user_id=am,
        kickoff=Kickoff(inputs={"run_kind": "flags", "timesheet_id": str(timesheet_id), "version": str(version)}),
        dedup_key=f"flags:{timesheet_id}:{version}",
    )  # fmt: skip
    return str(ref.run_id) if ref.run_id else ref.reason


__all__ = ["RUN_HOUR_LOCAL", "nudge_kickoff", "nudge_tick", "start_flag_run", "start_nudge_run"]
