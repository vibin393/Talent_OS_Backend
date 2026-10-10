# ruff: noqa: E501
"""Costs, metering and the daily cost cap (Epic 76, story 76.5, spec A6).

* ``today`` -- Sigma ``voice_calls.cost_estimate_minor`` of calls requested today (Tenant timezone) + reservations of the
  calls still open (``reserved_minor`` = ``max_call_minutes x rate``) + Sigma STT estimates.
* ``place_call`` takes ``pg_advisory_xact_lock(hashtext('telephony_cap:<tenant>'))`` before the cap check so two calls at
  the cap cannot both pass.
* Alerts: crossing ``cost_alert_pct`` -> ``telephony_cost_threshold`` (once per day); 100 % ->
  ``telephony_cost_cap_reached`` (mandatory). In-progress calls are never cut.
"""

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.telephony import OPEN_CALL_STATUSES, SttJob, TelephonySettings, VoiceCall
from app.services.telephony import common

_alerted: set[tuple[str, str, str]] = set()  # (tenant, day, kind) -- per process; the notification id dedups too


async def lock(db: AsyncSession) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"telephony_cap:{common.tenant_of(db)}"})


async def day_bounds(db: AsyncSession, day: date | None = None) -> tuple[datetime, datetime, date]:
    tz = await common.tenant_timezone(db)
    local_day = day or common.now().astimezone(tz).date()
    start = datetime.combine(local_day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1), local_day


async def today(db: AsyncSession) -> int:
    start, end, _ = await day_bounds(db)
    calls = (
        await db.execute(
            select(
                func.coalesce(func.sum(VoiceCall.cost_estimate_minor), 0),
            ).where(VoiceCall.requested_at >= start, VoiceCall.requested_at < end, VoiceCall.status.notin_(OPEN_CALL_STATUSES))
        )
    ).scalar_one()
    reserved = (
        await db.execute(
            select(func.coalesce(func.sum(VoiceCall.reserved_minor), 0)).where(
                VoiceCall.status.in_(OPEN_CALL_STATUSES), VoiceCall.requested_at >= start - timedelta(hours=2)
            )
        )
    ).scalar_one()
    stt = (
        await db.execute(
            select(func.coalesce(func.sum(SttJob.cost_estimate_minor), 0)).where(SttJob.created_at >= start, SttJob.created_at < end)
        )
    ).scalar_one()
    return int(calls) + int(reserved) + int(stt)


def pct(spent: int, cap: int) -> int:
    if cap <= 0:
        return 100 if spent > 0 else 0
    return min(int(spent * 100 // cap), 999)


async def check_alerts(db: AsyncSession, settings: TelephonySettings) -> None:
    """Notify on crossing the alert threshold (once a day) and at 100 % (mandatory)."""
    spent = await today(db)
    p = pct(spent, settings.daily_cost_cap_minor)
    _, _, day = await day_bounds(db)
    tenant_id = common.tenant_of(db)
    for kind, threshold in (("telephony_cost_cap_reached", 100), ("telephony_cost_threshold", settings.cost_alert_pct)):
        if p < threshold:
            continue
        key = (str(tenant_id), day.isoformat(), kind)
        if key in _alerted:
            continue
        _alerted.add(key)
        common.fan_out(tenant_id, kind, {"event_id": f"{kind}:{day.isoformat()}", "day": day.isoformat(), "pct": p})
        from app.core.events import emit
        from app.events.telephony import TelephonyCostThreshold

        emit(db, TelephonyCostThreshold(tenant_id=tenant_id, day=day.isoformat(), pct=p))
        break


def reset_alerts() -> None:
    _alerted.clear()


async def usage(db: AsyncSession, start: date, end: date, group_by: str) -> list[dict[str, Any]]:
    tz = await common.tenant_timezone(db)
    lo = datetime.combine(start, time.min, tzinfo=tz)
    hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)
    if group_by == "user":
        key = VoiceCall.placed_by
    elif group_by == "legal_entity":
        key = VoiceCall.legal_entity_id
    else:
        key = func.date(func.timezone(str(tz), VoiceCall.requested_at))
    rows = (
        await db.execute(
            select(
                key.label("k"),
                func.count(VoiceCall.id),
                func.coalesce(func.sum(VoiceCall.billable_minutes), 0),
                func.coalesce(func.sum(VoiceCall.cost_estimate_minor), 0),
            )
            .where(VoiceCall.requested_at >= lo, VoiceCall.requested_at < hi, VoiceCall.status != "refused")
            .group_by(key)
            .order_by(key)
        )
    ).all()
    stt_minutes: dict[str, int] = {}
    if group_by == "day":
        stt_rows = (
            await db.execute(
                select(
                    func.date(func.timezone(str(tz), SttJob.created_at)),
                    func.coalesce(func.sum(SttJob.billable_minutes), 0),
                )
                .where(SttJob.created_at >= lo, SttJob.created_at < hi)
                .group_by(func.date(func.timezone(str(tz), SttJob.created_at)))
            )
        ).all()
        stt_minutes = {str(k): int(v) for k, v in stt_rows}
    out = []
    for k, n, minutes, cost in rows:
        label = str(k) if k is not None else "default"
        out.append({"key": label, "calls": int(n), "minutes": int(minutes), "stt_minutes": stt_minutes.get(label, 0), "cost_estimate_minor": int(cost)})
    return out


async def user_names(db: AsyncSession, ids: list[uuid.UUID]) -> dict[str, str]:
    from app.models.identity import User

    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.name).where(User.id.in_(ids)))).all()
    return {str(i): n for i, n in rows}
