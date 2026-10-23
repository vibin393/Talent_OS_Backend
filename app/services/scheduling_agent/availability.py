# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Availability inputs of the slot engine (Epic 59 story 59.1).

* Free/busy only, through ``connections_service.free_busy`` (P1: own connected calendars, <= 50 users, <= 14 days per
  call -- longer horizons are fetched in 14-day chunks). Users without a usable calendar are ``unknown``. Event titles
  or content are never requested or stored.
* Working hours: ``interviewer_scheduling_prefs`` -> the agent config ``working_hours`` (foundation column) -> the
  tenant settings default (Mon-Fri 09:00-18:00).
* Holidays: the P2 holiday calendars (country / state scope) of the user's preference country / region, else the
  tenant's country (Known gap 3: P2 calendars are placement-scoped, so country/state calendars are read directly).
* Candidate time zone: the request's zone, prefilled from the Candidate's location (country / state -> IANA zone;
  India -> ``Asia/Kolkata``), else the tenant zone.
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.scheduling_agent import DEFAULT_WORKING_HOURS, InterviewerSchedulingPrefs
from app.services import entity_refs
from app.services.scheduling_agent.common import AGENT_KEY, as_user, settings_row, tenant_of
from app.services.scheduling_agent.slots import Person

FREE_BUSY_CHUNK = timedelta(days=14)

_COUNTRY_ZONES: dict[str, str] = {
    "IN": "Asia/Kolkata", "INDIA": "Asia/Kolkata", "GB": "Europe/London", "UK": "Europe/London",
    "UNITED KINGDOM": "Europe/London", "US": "America/New_York", "USA": "America/New_York",
    "UNITED STATES": "America/New_York", "SG": "Asia/Singapore", "SINGAPORE": "Asia/Singapore", "AE": "Asia/Dubai",
    "UAE": "Asia/Dubai", "UNITED ARAB EMIRATES": "Asia/Dubai", "AU": "Australia/Sydney", "AUSTRALIA": "Australia/Sydney",
    "DE": "Europe/Berlin", "GERMANY": "Europe/Berlin", "FR": "Europe/Paris", "FRANCE": "Europe/Paris",
    "NL": "Europe/Amsterdam", "NETHERLANDS": "Europe/Amsterdam", "CA": "America/Toronto", "CANADA": "America/Toronto",
    "JP": "Asia/Tokyo", "JAPAN": "Asia/Tokyo", "IE": "Europe/Dublin", "IRELAND": "Europe/Dublin",
}  # fmt: skip
_US_STATE_ZONES: dict[str, str] = {
    "CA": "America/Los_Angeles", "CALIFORNIA": "America/Los_Angeles", "WA": "America/Los_Angeles",
    "WASHINGTON": "America/Los_Angeles", "OR": "America/Los_Angeles", "NV": "America/Los_Angeles",
    "TX": "America/Chicago", "TEXAS": "America/Chicago", "IL": "America/Chicago", "ILLINOIS": "America/Chicago",
    "CO": "America/Denver", "COLORADO": "America/Denver", "AZ": "America/Phoenix", "ARIZONA": "America/Phoenix",
}  # fmt: skip
_ZONE_COUNTRY: dict[str, str] = {
    "Asia/Kolkata": "IN", "Asia/Calcutta": "IN", "Europe/London": "GB", "Asia/Singapore": "SG", "Asia/Dubai": "AE",
}  # fmt: skip


def country_for_zone(tz: str | None) -> str | None:
    if not tz:
        return None
    if tz in _ZONE_COUNTRY:
        return _ZONE_COUNTRY[tz]
    if tz.startswith("America/"):
        return "US"
    return None


def zone_for_location(location: dict[str, Any] | None) -> str | None:
    if not location:
        return None
    country = str(location.get("country") or "").strip().upper()
    state = str(location.get("state") or "").strip().upper()
    if country in ("US", "USA", "UNITED STATES") and state in _US_STATE_ZONES:
        return _US_STATE_ZONES[state]
    return _COUNTRY_ZONES.get(country)


async def tenant_timezone(db: AsyncSession) -> str:
    info = await entity_refs.tenant(db, tenant_of(db))
    return info.timezone or "Asia/Kolkata"


async def candidate_timezone(db: AsyncSession, candidate_id: uuid.UUID) -> tuple[str, str]:
    """``(zone, source)`` -- source ``location`` | ``tenant``. Never returns the address."""
    from app.models.candidates import Candidate

    cand = await db.get(Candidate, candidate_id)
    zone = zone_for_location(cand.location if cand is not None else None)
    if zone is not None:
        return zone, "location"
    return await tenant_timezone(db), "tenant"


async def prefs_map(
    db: AsyncSession, user_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, InterviewerSchedulingPrefs]:
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return {}
    rows = await db.execute(
        select(InterviewerSchedulingPrefs).where(InterviewerSchedulingPrefs.user_id.in_(ids))
    )
    return {r.user_id: r for r in rows.scalars()}


async def default_working_hours(db: AsyncSession) -> dict[str, Any]:
    from app.models.agent_runtime import AgentConfig

    config = (
        await db.execute(select(AgentConfig).where(AgentConfig.agent_key == AGENT_KEY))
    ).scalar_one_or_none()
    if config is not None and config.working_hours:
        return dict(config.working_hours)
    settings = await settings_row(db)
    return dict(settings.default_working_hours or DEFAULT_WORKING_HOURS)


async def user_timezone(db: AsyncSession, user_id: uuid.UUID) -> str:
    prefs = (await prefs_map(db, [user_id])).get(user_id)
    return prefs.timezone if prefs is not None else await tenant_timezone(db)


async def holidays(
    db: AsyncSession, country: str | None, region: str | None, start: date, end: date
) -> frozenset[date]:
    """National + state holidays of the country (and region) calendars covering ``[start, end]``."""
    from app.models.time import Holiday, HolidayCalendar

    if not country:
        return frozenset()
    years = list(range(start.year, end.year + 1))
    cal_filter = (HolidayCalendar.scope_kind == "country") & (HolidayCalendar.country_code == country)
    if region:
        cal_filter = cal_filter | (
            (HolidayCalendar.scope_kind == "state") & (HolidayCalendar.state_code == region[:2].upper())
        )
    rows = await db.execute(
        select(Holiday.holiday_date)
        .join(HolidayCalendar, HolidayCalendar.id == Holiday.calendar_id)
        .where(
            cal_filter,
            HolidayCalendar.year.in_(years),
            HolidayCalendar.archived_at.is_(None),
            Holiday.type.in_(("national", "state")),
            Holiday.holiday_date >= start,
            Holiday.holiday_date <= end,
        )
    )
    return frozenset(r[0] for r in rows.all())


async def holiday_days(
    db: AsyncSession, country: str | None, region: str | None, start: date, end: date
) -> list[dict[str, Any]]:
    from app.models.time import Holiday, HolidayCalendar

    if not country:
        return []
    rows = await db.execute(
        select(Holiday.holiday_date, Holiday.name, HolidayCalendar.scope_kind, HolidayCalendar.state_code)
        .join(HolidayCalendar, HolidayCalendar.id == Holiday.calendar_id)
        .where(
            ((HolidayCalendar.scope_kind == "country") & (HolidayCalendar.country_code == country))
            | (
                (HolidayCalendar.scope_kind == "state")
                & (HolidayCalendar.state_code == (region or "")[:2].upper())
            ),
            HolidayCalendar.archived_at.is_(None),
            Holiday.type.in_(("national", "state")),
            Holiday.holiday_date >= start,
            Holiday.holiday_date <= end,
        )
        .order_by(Holiday.holiday_date)
    )
    return [
        {"date": r[0], "name": r[1], "applies_to": "country" if r[2] == "country" else f"state:{r[3]}"}
        for r in rows.all()
    ]


async def calendar_connected(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    from app.services import connections_service

    return {u for u in dict.fromkeys(user_ids) if await connections_service.has_active(db, u)}


async def free_busy(
    ctx: RequestContext, db: AsyncSession, user_ids: Sequence[uuid.UUID], start: datetime, end: datetime
) -> dict[uuid.UUID, tuple[str, list[tuple[datetime, datetime]]]]:
    """``{user: (status, busy blocks)}`` -- status ``available`` | ``unknown``; chunked at 14 days."""
    from app.schemas.connections import FreeBusyIn
    from app.services import connections_service

    ids = list(dict.fromkeys(user_ids))
    out: dict[uuid.UUID, tuple[str, list[tuple[datetime, datetime]]]] = {u: ("available", []) for u in ids}
    if not ids:
        return out
    uctx = as_user(ctx)
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + FREE_BUSY_CHUNK, end)
        result = await connections_service.free_busy(
            uctx, db, FreeBusyIn(user_ids=ids[:50], start_utc=cursor, end_utc=chunk_end)
        )
        for item in result.items:
            status, busy = out.get(item.user_id, ("available", []))
            if item.status != "available":
                status = "unknown"
            busy.extend((b.start.astimezone(UTC), b.end.astimezone(UTC)) for b in item.busy)
            out[item.user_id] = (status, busy)
        cursor = chunk_end
    return out


async def booked_per_day(
    db: AsyncSession,
    user_ids: Sequence[uuid.UUID],
    start: datetime,
    end: datetime,
    zones: dict[uuid.UUID, str],
) -> dict[uuid.UUID, dict[date, int]]:
    """Scheduled interviews per participant and local day (for ``max_interviews_per_day``)."""
    from app.models.interviews import Interview, InterviewParticipant

    out: dict[uuid.UUID, dict[date, int]] = {u: {} for u in user_ids}
    if not user_ids:
        return out
    rows = await db.execute(
        select(InterviewParticipant.user_id, Interview.starts_at)
        .join(Interview, Interview.id == InterviewParticipant.interview_id)
        .where(
            InterviewParticipant.user_id.in_(list(user_ids)),
            InterviewParticipant.removed_at.is_(None),
            Interview.status == "scheduled",
            Interview.starts_at >= start - timedelta(days=1),
            Interview.starts_at <= end + timedelta(days=1),
        )
    )
    for uid, starts_at in rows.all():
        try:
            tz = ZoneInfo(zones.get(uid) or "UTC")
        except Exception:
            tz = ZoneInfo("UTC")
        d = starts_at.astimezone(tz).date()
        out.setdefault(uid, {})[d] = out.setdefault(uid, {}).get(d, 0) + 1
    return out


async def people(
    ctx: RequestContext,
    db: AsyncSession,
    user_ids: Sequence[uuid.UUID],
    start: datetime,
    end: datetime,
    *,
    exclude_interview_id: uuid.UUID | None = None,
) -> list[Person]:
    """The engine's view of each participant over ``[start, end]``."""
    ids = list(dict.fromkeys(user_ids))
    prefs = await prefs_map(db, ids)
    default_hours = await default_working_hours(db)
    tenant_tz = await tenant_timezone(db)
    tenant_country = country_for_zone(tenant_tz) or "IN"
    fb = await free_busy(ctx, db, ids, start, end)
    zones = {u: (prefs[u].timezone if u in prefs else tenant_tz) for u in ids}
    per_day = await booked_per_day(db, ids, start, end, zones)
    if exclude_interview_id is not None:
        per_day = await _minus_interview(db, per_day, exclude_interview_id, zones)
    out: list[Person] = []
    for uid in ids:
        p = prefs.get(uid)
        country = p.holiday_country if p is not None and p.holiday_country else tenant_country
        region = p.holiday_region if p is not None else None
        hol = await holidays(
            db, country, region, (start - timedelta(days=1)).date(), (end + timedelta(days=1)).date()
        )
        status, busy = fb.get(uid, ("unknown", []))
        out.append(
            Person(
                user_id=uid, timezone=zones[uid], working_hours=(p.working_hours if p is not None else default_hours),
                holidays=hol, busy=tuple(sorted(busy)), known=status == "available",
                buffer_minutes=(p.buffer_minutes or 0) if p is not None else 0,
                max_per_day=p.max_interviews_per_day if p is not None else None, booked_per_day=per_day.get(uid, {}),
            )
        )  # fmt: skip
    return out


async def _minus_interview(
    db: AsyncSession,
    per_day: dict[uuid.UUID, dict[date, int]],
    interview_id: uuid.UUID,
    zones: dict[uuid.UUID, str],
) -> dict[uuid.UUID, dict[date, int]]:
    """A reschedule does not count the interview being moved toward the daily cap."""
    from app.models.interviews import Interview

    row = await db.get(Interview, interview_id)
    if row is None:
        return per_day
    for uid, days in per_day.items():
        try:
            d = row.starts_at.astimezone(ZoneInfo(zones.get(uid) or "UTC")).date()
        except Exception:  # nosec B112 - best-effort loop; a failed item is intentionally skipped
            continue
        if days.get(d):
            days[d] -= 1
    return per_day


async def organiser_timezone(db: AsyncSession, owner_user_id: uuid.UUID) -> str:
    return await user_timezone(db, owner_user_id)


async def business_holidays(
    db: AsyncSession, owner_user_id: uuid.UUID, start: date, end: date
) -> frozenset[date]:
    prefs = (await prefs_map(db, [owner_user_id])).get(owner_user_id)
    country = (
        prefs.holiday_country
        if prefs is not None and prefs.holiday_country
        else (country_for_zone(await tenant_timezone(db)) or "IN")
    )
    return await holidays(db, country, prefs.holiday_region if prefs is not None else None, start, end)


async def user_names(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    infos = await entity_refs.users(db, list(user_ids))
    return {k: v.name for k, v in infos.items()}


async def scheduled_count(db: AsyncSession, application_id: uuid.UUID) -> int:
    from app.models.interviews import Interview

    return int(
        (
            await db.execute(
                select(func.count()).where(
                    Interview.application_id == application_id, Interview.status == "scheduled"
                )
            )
        ).scalar_one()
    )


__all__ = [
    "business_holidays",
    "calendar_connected",
    "candidate_timezone",
    "country_for_zone",
    "default_working_hours",
    "free_busy",
    "holiday_days",
    "holidays",
    "organiser_timezone",
    "people",
    "prefs_map",
    "tenant_timezone",
    "user_names",
    "user_timezone",
    "zone_for_location",
]
