# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Shared helpers of the telephony package (Epic 76): errors, clock, phone normalisation, the tenant timezone and the
calling user's id. No phone number, token or recording URL is ever logged."""

import hashlib
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.permissions import Principal

PERM_CALL = "voice:call"
PERM_UPLOAD = "voice:upload"
PERM_MANAGE = "integration:manage"
PERM_VIEW = "integration:view"
CATEGORY = "voice"
TELEPHONY_PROVIDERS = ("exotel", "twilio", "plivo")

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_clock: Callable[[], datetime] | None = None


def now() -> datetime:
    return _clock() if _clock is not None else datetime.now(UTC)


def set_clock(fn: Callable[[], datetime] | None) -> None:
    """Tests: freeze the telephony clock (calling hours, cost day)."""
    global _clock
    _clock = fn


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def not_found(what: str = "Call") -> NotFound:
    return NotFound(f"{what} not found")


def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def user_id_of(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return p.id if isinstance(p, Principal) and p.type == "user" else None


def normalize_e164(value: str | None, *, default_country: str = "91") -> str | None:
    """Normalise a phone to E.164 (Indian 10-digit numbers get +91). None when not callable."""
    if not value:
        return None
    raw = re.sub(r"[\s\-().]", "", str(value))
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if not raw.startswith("+"):
        digits = raw.lstrip("0")
        if len(digits) == 10 and digits[0] in "6789":
            raw = f"+{default_country}{digits}"
        elif len(digits) == 12 and digits.startswith(default_country):
            raw = "+" + digits
        else:
            return None
    return raw if _E164.match(raw) else None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def tenant_timezone(db: AsyncSession) -> ZoneInfo:
    from app.models.identity import Tenant

    name = (await db.execute(select(Tenant.timezone).where(Tenant.id == tenant_of(db)))).scalar_one_or_none()
    return zone(name)


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Asia/Kolkata")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Kolkata")


def fan_out(tenant_id: uuid.UUID, type_key: str, payload: dict[str, Any]) -> None:
    """Notifications are queued (never block the call path)."""
    from loguru import logger

    from app.tasks import notifications as notification_tasks
    from app.tasks.runtime import enqueue

    try:
        enqueue(notification_tasks.fan_out, str(tenant_id), type_key, payload)
    except Exception:  # pragma: no cover - logged
        logger.exception("Telephony notification {} failed", type_key)
