# ruff: noqa: E501, ARG001
"""Marketing settings (Epic 39 A1) and recipient time zones.

User decisions (2026-10-06, binding): frequency cap 3 per rolling 7 days (a tenant may only lower the count); send
windows 08:00-21:00 recipient-local for email and 09:00-21:00 for SMS / WhatsApp (a tenant may only narrow them);
last-touch attribution over 30 days (fixed). One row per tenant, created lazily with the defaults; changes are
audited (``AuditedMixin``). ``resume`` clears a tenant-level auto-pause (``marketing:configure``).
"""

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.settings import get_settings
from app.models.campaigns import CHANNELS, DEFAULT_SEND_WINDOWS, Campaign, MarketingSettings
from app.schemas.campaigns import MarketingSettingsOut, MarketingSettingsUpdate
from app.services.campaigns.common import now, tenant_of, user_id_of

MAX_CAP_COUNT = 3
CAP_DAYS = 7
ATTRIBUTION_DAYS = 30

# Recipient time zone from ``candidates.location.country`` (ISO code or common name), else the tenant default.
COUNTRY_ZONES: dict[str, str] = {
    "IN": "Asia/Kolkata", "INDIA": "Asia/Kolkata",
    "AE": "Asia/Dubai", "UAE": "Asia/Dubai", "UNITED ARAB EMIRATES": "Asia/Dubai",
    "GB": "Europe/London", "UK": "Europe/London", "UNITED KINGDOM": "Europe/London",
    "US": "America/New_York", "USA": "America/New_York", "UNITED STATES": "America/New_York",
    "SG": "Asia/Singapore", "SINGAPORE": "Asia/Singapore",
    "AU": "Australia/Sydney", "AUSTRALIA": "Australia/Sydney",
    "CA": "America/Toronto", "CANADA": "America/Toronto",
    "DE": "Europe/Berlin", "GERMANY": "Europe/Berlin",
    "NL": "Europe/Amsterdam", "NETHERLANDS": "Europe/Amsterdam",
    "SA": "Asia/Riyadh", "SAUDI ARABIA": "Asia/Riyadh",
    "QA": "Asia/Qatar", "QATAR": "Asia/Qatar",
    "MY": "Asia/Kuala_Lumpur", "MALAYSIA": "Asia/Kuala_Lumpur",
    "JP": "Asia/Tokyo", "JAPAN": "Asia/Tokyo",
    "FR": "Europe/Paris", "FRANCE": "Europe/Paris",
    "IE": "Europe/Dublin", "IRELAND": "Europe/Dublin",
    "NZ": "Pacific/Auckland", "NEW ZEALAND": "Pacific/Auckland",
    "PH": "Asia/Manila", "PHILIPPINES": "Asia/Manila",
    "LK": "Asia/Colombo", "SRI LANKA": "Asia/Colombo",
    "BD": "Asia/Dhaka", "BANGLADESH": "Asia/Dhaka",
    "NP": "Asia/Kathmandu", "NEPAL": "Asia/Kathmandu",
}  # fmt: skip


def platform_max_rate() -> dict[str, int]:
    s = get_settings()
    return {
        "email": s.marketing_max_rate_email,
        "sms": s.marketing_max_rate_sms,
        "whatsapp": s.marketing_max_rate_whatsapp,
    }


def platform_max_daily() -> dict[str, int]:
    s = get_settings()
    return {
        "email": s.marketing_max_daily_email,
        "sms": s.marketing_max_daily_sms,
        "whatsapp": s.marketing_max_daily_whatsapp,
    }


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Asia/Kolkata")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Kolkata")


def zone_for_country(country: str | None, default_tz: str | None) -> ZoneInfo:
    key = (country or "").strip().upper()
    return zone(COUNTRY_ZONES.get(key) or default_tz)


def parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


async def get_row(db: AsyncSession) -> MarketingSettings:
    tid = tenant_of(db)
    row = await db.get(MarketingSettings, tid)
    if row is None:
        from app.services import directory_service

        row = MarketingSettings(
            tenant_id=tid,
            send_windows={k: dict(v) for k, v in DEFAULT_SEND_WINDOWS.items()},
            rate_per_minute={},
            daily_cap={},
            default_timezone=await directory_service.tenant_timezone(db, tid),
        )
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


def effective_windows(row: MarketingSettings) -> dict[str, tuple[time, time]]:
    out: dict[str, tuple[time, time]] = {}
    for ch in CHANNELS:
        w = (row.send_windows or {}).get(ch) or DEFAULT_SEND_WINDOWS[ch]
        try:
            out[ch] = (parse_hhmm(w["start"]), parse_hhmm(w["end"]))
        except (KeyError, ValueError):
            d = DEFAULT_SEND_WINDOWS[ch]
            out[ch] = (parse_hhmm(d["start"]), parse_hhmm(d["end"]))
    return out


def effective_rate(row: MarketingSettings, channel: str) -> int:
    cap = platform_max_rate()[channel]
    value = (row.rate_per_minute or {}).get(channel)
    return min(int(value), cap) if value else cap


def effective_daily_cap(row: MarketingSettings, channel: str) -> int:
    cap = platform_max_daily()[channel]
    value = (row.daily_cap or {}).get(channel)
    return min(int(value), cap) if value else cap


def out(row: MarketingSettings) -> MarketingSettingsOut:
    windows = effective_windows(row)
    return MarketingSettingsOut(
        frequency_cap_count=row.frequency_cap_count,
        frequency_cap_days=row.frequency_cap_days,
        send_windows={
            ch: {"start": w[0].strftime("%H:%M"), "end": w[1].strftime("%H:%M")} for ch, w in windows.items()
        },  # type: ignore[misc]
        approval_threshold=row.approval_threshold,
        ab_window_hours=row.ab_window_hours,
        rate_per_minute={ch: effective_rate(row, ch) for ch in CHANNELS},
        daily_cap={ch: effective_daily_cap(row, ch) for ch in CHANNELS},
        platform_max_rate=platform_max_rate(),
        platform_max_daily=platform_max_daily(),
        attribution_window_days=row.attribution_window_days,
        default_timezone=row.default_timezone,
        footer_contact=row.footer_contact,
        paused_at=row.paused_at,
        paused_reason=row.paused_reason,
    )


async def get_settings_out(ctx: RequestContext, db: AsyncSession) -> MarketingSettingsOut:
    return out(await get_row(db))


def _invalid(field: str, message: str) -> ValidationFailed:
    return ValidationFailed(message, code="invalid_marketing_settings", details={"fields": {field: message}})


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: MarketingSettingsUpdate
) -> MarketingSettingsOut:
    row = await get_row(db)
    if data.frequency_cap_count is not None:
        if data.frequency_cap_count > MAX_CAP_COUNT:
            raise _invalid("frequency_cap_count", "The frequency cap cannot exceed 3 messages per 7 days")
        row.frequency_cap_count = data.frequency_cap_count
    if data.send_windows is not None:
        merged = {k: dict(v) for k, v in (row.send_windows or DEFAULT_SEND_WINDOWS).items()}
        for ch, w in data.send_windows.items():
            start, end = parse_hhmm(w.start), parse_hhmm(w.end)
            d_start, d_end = (
                parse_hhmm(DEFAULT_SEND_WINDOWS[ch]["start"]),
                parse_hhmm(DEFAULT_SEND_WINDOWS[ch]["end"]),
            )
            if not start < end:
                raise _invalid(f"send_windows.{ch}", "The window must start before it ends")
            if start < d_start or end > d_end:
                raise _invalid(
                    f"send_windows.{ch}",
                    f"The {ch} window must stay within {DEFAULT_SEND_WINDOWS[ch]['start']}-{DEFAULT_SEND_WINDOWS[ch]['end']}",
                )
            merged[ch] = {"start": w.start, "end": w.end}
        row.send_windows = merged
    if data.approval_threshold is not None:
        row.approval_threshold = data.approval_threshold
    if data.ab_window_hours is not None:
        row.ab_window_hours = data.ab_window_hours
    for field, maxima in (("rate_per_minute", platform_max_rate()), ("daily_cap", platform_max_daily())):
        value = getattr(data, field)
        if value is None:
            continue
        merged_nums: dict[str, Any] = dict(getattr(row, field) or {})
        for ch in CHANNELS:
            n = getattr(value, ch)
            if n is None:
                continue
            if n > maxima[ch]:
                raise _invalid(f"{field}.{ch}", f"At most {maxima[ch]} (platform maximum)")
            merged_nums[ch] = n
        setattr(row, field, merged_nums)
    if data.default_timezone is not None:
        try:
            ZoneInfo(data.default_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise _invalid("default_timezone", "Unknown time zone") from exc
        row.default_timezone = data.default_timezone
    if data.footer_contact is not None:
        row.footer_contact = data.footer_contact or None
    await db.flush()
    return out(row)


async def pause_tenant(db: AsyncSession, reason: str, by: Any = None) -> bool:
    """Tenant-level pause (reputation guard or manual): every ``sending|scheduled`` campaign -> ``paused``."""
    row = await get_row(db)
    if row.paused_at is not None:
        return False
    row.paused_at, row.paused_reason, row.paused_by = now(), reason, by
    await db.execute(
        update(Campaign)
        .where(Campaign.status.in_(("sending", "scheduled")))
        .values(status="paused", updated_at=now())
    )
    await db.flush()
    audit.record("marketing.paused", "marketing_settings", row.tenant_id, session=db, reason=reason)
    return True


async def pause(ctx: RequestContext, db: AsyncSession, reason: str) -> MarketingSettingsOut:
    await pause_tenant(db, "manual", user_id_of(ctx))
    return out(await get_row(db))


async def resume(ctx: RequestContext, db: AsyncSession, reason: str) -> MarketingSettingsOut:
    row = await get_row(db)
    if row.paused_at is not None:
        row.paused_at, row.paused_reason, row.paused_by = None, None, None
        await db.flush()
        audit.record(
            "marketing.resumed",
            "marketing_settings",
            row.tenant_id,
            session=db,
            reason=reason[:500],
            by=str(user_id_of(ctx)),
        )
    return out(row)


def local_now(at: datetime, tz: ZoneInfo) -> datetime:
    return at.astimezone(tz)


async def campaign_ids_paused(db: AsyncSession) -> list[Any]:
    return list((await db.execute(select(Campaign.id).where(Campaign.status == "paused"))).scalars())
