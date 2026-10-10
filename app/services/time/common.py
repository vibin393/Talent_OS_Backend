"""Shared helpers of the ``time`` domain: errors, strict decimal parsing, settings row, lookups."""

import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.placements import Placement
from app.models.time import TimeSettings, Timesheet

HOURS_RE = re.compile(r"^\d{1,2}(\.\d{1,2})?$")
SIGNED_HOURS_RE = re.compile(r"^-?\d{1,2}(\.\d{1,2})?$")
DAY_MAX = Decimal("24.00")
ZERO = Decimal("0.00")


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(ctx: RequestContext, db: AsyncSession) -> uuid.UUID:
    if ctx.tenant_id is not None:
        return ctx.tenant_id
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def not_found(what: str = "Timesheet") -> NotFound:
    return NotFound(f"{what} not found")


def not_editable(status: str) -> ConflictError:
    return ConflictError(
        "This timesheet can no longer be edited", code="timesheet_not_editable", details={"status": status}
    )


def parse_hours(raw: str | None, *, signed: bool = False) -> Decimal | None:
    r"""Strict hours parsing (never rounds user input): ``\d{1,2}(.\d{1,2})?``; comma decimals are refused.
    Returns ``None`` for a bad value."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not (SIGNED_HOURS_RE if signed else HOURS_RE).match(text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:  # pragma: no cover - the regex guards it
        return None


def dec(value: Any) -> Decimal:
    """Decimal from a stored JSON string / number (multipliers)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        msg = "float in a time path"
        raise TypeError(msg)
    return Decimal(str(value))


async def settings_row(db: AsyncSession, tenant_id: uuid.UUID) -> TimeSettings:
    row = await db.get(TimeSettings, tenant_id)
    if row is None:
        row = TimeSettings(tenant_id=tenant_id)
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


async def get_placement(db: AsyncSession, placement_id: uuid.UUID) -> Placement:
    p = (await db.execute(select(Placement).where(Placement.id == placement_id))).scalar_one_or_none()
    if p is None:
        raise NotFound("Placement not found")
    return p


async def get_timesheet(db: AsyncSession, timesheet_id: uuid.UUID, *, lock: bool = False) -> Timesheet:
    stmt = select(Timesheet).where(Timesheet.id == timesheet_id)
    if lock:
        stmt = stmt.with_for_update()
    ts = (await db.execute(stmt)).scalar_one_or_none()
    if ts is None:
        raise not_found()
    return ts


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details)


def fmt_time(value: Any) -> str | None:
    return value.strftime("%H:%M") if value is not None else None


def today() -> date:
    from app.core.fiscal import today_for

    return today_for("Asia/Kolkata")
