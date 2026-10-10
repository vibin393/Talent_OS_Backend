"""Time (Epic 25) Celery tasks. Every task starts with ``tenant_id`` (except fan-outs).

* ``time.placement_event(tenant_id, placement_id)`` (queue ``interactive``): ``PlacementCreated`` /
  ``PlacementActivated`` -> config version 1 and the period Timesheets so far (idempotent).
* ``time.rate_segment_added(tenant_id, placement_id, segment_id, superseded)`` (queue ``interactive``):
  retro rate adjustments on posted hours (idempotent per ledger / source / segment).
* ``time.daily_all`` (beat, daily 00:30 IST) -> ``time.daily(tenant_id)`` (queue ``maintenance``): create
period
  Timesheets, scheduled reminders, missing flags and the approvers' pending digest.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids


async def _placement_event(tenant_id: uuid.UUID, placement_id: uuid.UUID) -> int:
    from app.models.placements import Placement
    from app.services.time import configs, timesheets

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        p = (await db.execute(select(Placement).where(Placement.id == placement_id))).scalar_one_or_none()
        if p is None:
            return 0
        await configs.ensure_config(ctx, db, p)
        return len(await timesheets.create_period_timesheets(ctx, db, p))


@celery_app.task(name="time.placement_event", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def placement_event(tenant_id: str, placement_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _placement_event(tid, uuid.UUID(str(placement_id))), tenant_id=tid))


async def _rate_segment_added(
    tenant_id: uuid.UUID, placement_id: uuid.UUID, segment_id: uuid.UUID, superseded: Any
) -> int:
    from app.services.time import rate_adjustments

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        return await rate_adjustments.handle_segment_added(ctx, db, placement_id, segment_id, superseded)


@celery_app.task(name="time.rate_segment_added", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def rate_segment_added(tenant_id: str, placement_id: str, segment_id: str, superseded: list[Any]) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(
        run(
            lambda: _rate_segment_added(
                tid, uuid.UUID(str(placement_id)), uuid.UUID(str(segment_id)), superseded
            ),
            tenant_id=tid,
        )
    )


async def _daily(tenant_id: uuid.UUID) -> dict[str, int]:
    from app.models.placements import Placement
    from app.services.time import monitoring, timesheets

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    created = 0
    async with tenant_session(tenant_id) as db:
        for p in (
            await db.execute(select(Placement).where(Placement.status.in_(("active", "offboarding"))))
        ).scalars():
            try:
                async with db.begin_nested():
                    created += len(await timesheets.create_period_timesheets(ctx, db, p))
            except Exception:
                logger.exception("period timesheets failed for placement {}", p.id)
    async with tenant_session(tenant_id) as db:
        reminded = await monitoring.scheduled_reminders(ctx, db)
        missing = await monitoring.flag_missing(ctx, db)
        digests = await monitoring.tenant_pending_digest(ctx, db)
    return {"created": created, "reminded": reminded, "missing": missing, "digests": digests}


@celery_app.task(name="time.daily", queue="maintenance", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily(tenant_id: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _daily(tid), tenant_id=tid))


@celery_app.task(name="time.daily_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(daily, str(tid))
    return len(ids)


# ================================================================ Epic 26: Leave & Holidays
# * ``time.leave_daily_all`` (beat, daily 00:45 IST) -> ``time.leave_daily(tenant_id)`` (queue ``maintenance``):
#   monthly accrual of the previous month on the tenant's ``accrual_day``, year-end carry-forward on the first
#   day of the leave year, and the nightly ledger / cache verification.
# * ``time.leave_calendar_changed(tenant_id, calendar_id)`` (queue ``interactive``): re-prefill draft Timesheets.
async def _leave_daily(tenant_id: uuid.UUID, on: Any = None) -> dict[str, int]:
    from datetime import date, timedelta

    from app.services.time import accruals, leave_balances
    from app.services.time.common import today
    from app.services.time.holidays import leave_settings_row

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    day: date = on or today()
    out = {"accrued": 0, "year_end": 0, "mismatches": 0}
    async with tenant_session(tenant_id) as db:
        settings = await leave_settings_row(db, tenant_id)
        mode, accrual_day = settings.leave_year, settings.accrual_day
    if day.day == accrual_day:
        prev = day.replace(day=1) - timedelta(days=1)
        async with tenant_session(tenant_id) as db:
            out["accrued"] = await leave_balances.run_monthly_accruals(ctx, db, prev.replace(day=1))
    start, _ = accruals.leave_year_bounds(accruals.leave_year_of(day, mode), mode)
    if day == start:
        old = accruals.leave_year_of(day - timedelta(days=1), mode)
        async with tenant_session(tenant_id) as db:
            out["year_end"] = await leave_balances.run_year_end(ctx, db, old)
    async with tenant_session(tenant_id) as db:
        out["mismatches"] = len((await leave_balances.verify_balances(ctx, db)).mismatches)
    return out


@celery_app.task(name="time.leave_daily", queue="maintenance", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def leave_daily(tenant_id: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _leave_daily(tid), tenant_id=tid))


@celery_app.task(name="time.leave_daily_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def leave_daily_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(leave_daily, str(tid))
    return len(ids)


async def _leave_calendar_changed(tenant_id: uuid.UUID, calendar_id: uuid.UUID) -> int:
    from app.services.time import holidays, leave_requests

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    n = 0
    async with tenant_session(tenant_id) as db:
        for pid, ps, pe in await holidays.placements_using(db, calendar_id):
            try:
                async with db.begin_nested():
                    n += await leave_requests.refresh_drafts(ctx, db, pid, ps, pe)
            except Exception:
                logger.exception("holiday prefill refresh failed for placement {}", pid)
    return n


@celery_app.task(name="time.leave_calendar_changed", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def leave_calendar_changed(tenant_id: str, calendar_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _leave_calendar_changed(tid, uuid.UUID(str(calendar_id))), tenant_id=tid))
