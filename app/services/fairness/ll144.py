# ruff: noqa: E501
"""NYC LL144 candidate notice (Epic 49 A8, FR-248, FR-249).

A Demand flagged for NYC publishes the notice at ``notice_published_at``; the AEDT may be *used* for that Demand only
after ``notice_lead_business_days`` (>= 10) business days of the tenant calendar (``core.business_hours``; a tenant
without a configured work week counts Monday-Friday). Until then the score suppressor hides AI Match / AI Screening /
auto-score outputs for that Demand ("Hidden -- notice period").
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import business_hours
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.fairness import AedtJurisdictionFlag
from app.schemas.fairness import Ll144FlagOut, PublicAedtOut, PublicNoticeOut
from app.services.fairness import inventory
from app.services.fairness import settings as settings_mod
from app.services.fairness.common import now, tenant_of, user_id


def add_business_days(start: date, days: int, cal: business_hours.Calendar) -> date:
    """Return the date of the ``days``-th working day after ``start`` (Mon-Fri when the calendar has no working day)."""
    has_week = bool(cal.work_week)
    day, counted = start, 0
    for _ in range(3660):
        day += timedelta(days=1)
        working = cal.window(day) is not None if has_week else (day.weekday() < 5 and day not in cal.holidays)
        if working:
            counted += 1
            if counted >= days:
                return day
    return start + timedelta(days=days)  # pragma: no cover


async def period_end(db: AsyncSession, published_at: datetime, lead_days: int) -> datetime:
    """Notice period end: midnight (tenant time zone) after the ``lead_days``-th full business day."""
    cal, tz = await business_hours.tenant_calendar(db)
    zone = ZoneInfo(tz)
    last = add_business_days(published_at.astimezone(zone).date(), lead_days, cal)
    return datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(UTC)


async def flags_for(db: AsyncSession, demand_ids: list[uuid.UUID]) -> dict[uuid.UUID, AedtJurisdictionFlag]:
    if not demand_ids:
        return {}
    rows = (
        await db.execute(select(AedtJurisdictionFlag).where(AedtJurisdictionFlag.demand_id.in_(demand_ids)))
    ).scalars()
    return {r.demand_id: r for r in rows}


async def _out(db: AsyncSession, demand_id: uuid.UUID, flag: AedtJurisdictionFlag | None) -> Ll144FlagOut:
    s = await settings_mod.current(db)
    ends = await period_end(db, flag.notice_published_at, s.notice_lead_business_days) if flag else None
    return Ll144FlagOut(
        demand_id=demand_id,
        flagged=flag is not None,
        jurisdiction="nyc" if flag else None,
        notice_published_at=flag.notice_published_at if flag else None,
        notice_period_ends_at=ends,
        in_notice_period=bool(ends and now() < ends),
        ll144_enabled=s.ll144_enabled,
    )


async def _demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> None:
    from app.services.demands.matching_port import get_demand

    await get_demand(ctx, db, demand_id)  # 404 outside the caller's demand read scope


async def get_flag(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> Ll144FlagOut:
    await _demand(ctx, db, demand_id)
    return await _out(db, demand_id, await db.get(AedtJurisdictionFlag, demand_id))


async def set_flag(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, flagged: bool
) -> Ll144FlagOut:
    await _demand(ctx, db, demand_id)
    flag = await db.get(AedtJurisdictionFlag, demand_id)
    if flagged and flag is None:
        flag = AedtJurisdictionFlag(
            demand_id=demand_id,
            tenant_id=tenant_of(db),
            jurisdiction="nyc",
            notice_published_at=now(),
            flagged_by=user_id(ctx),
        )
        db.add(flag)
        await db.flush()
    elif not flagged and flag is not None:
        await db.delete(flag)
        await db.flush()
        flag = None
    return await _out(db, demand_id, flag)


async def public_notice(slug: str, posting_id: uuid.UUID) -> PublicNoticeOut:
    from app.services.portal.careers import live_posting, resolve_site

    site = await resolve_site(slug)
    async with tenant_session(site.tenant_id) as db:
        posting = await live_posting(db, posting_id)
        s = await settings_mod.current(db)
        flag = await db.get(AedtJurisdictionFlag, posting["demand_id"])
        if not s.ll144_enabled or flag is None:
            return PublicNoticeOut(applies=False)
        entries = await inventory.active_entries(db)
        from app.services.fairness import aedt_p4  # Epic 65: the agents in use (FR-323)

        agent_lines = aedt_p4.notice_lines(entries)
        notice_text = "\n\n".join(t for t in (s.ll144_notice_text, *agent_lines) if t) or None
        return PublicNoticeOut(
            applies=True,
            notice_text=notice_text,
            qualifications_text=s.qualifications_text,
            data_retention_text=s.data_retention_text,
            alternative_process_text=s.alternative_process_text,
            aedts=[
                PublicAedtOut(name=e.name, feature=e.feature, version=e.version, data_used=e.data_used)
                for e in entries
            ],
        )
