# ruff: noqa: D401, E501
"""Shared helpers of the ``mobility`` domain (Epic 41): permission keys, errors, clock, the tenant settings row,
system contexts and the pipeline-guard bypass used by mobility's own Application moves."""

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, NotFound, ValidationFailed
from app.core.fiscal import today_for
from app.core.permissions import Principal, forbidden
from app.models.mobility import MobilitySettings

MANAGE = "mobility:manage"
TRANSFER = "mobility:transfer"
APPROVAL_SUBJECT = "internal_application"
HUMAN_ONLY_TARGET = "transfer_confirmed"
REJECT_REASON_CODE = "internal_manager_rejected"
REJECT_REASON_LABEL = "Internal: manager did not approve"
TZ = "Asia/Kolkata"
OPEN_PLACEMENT_STATUSES = ("pre_joining", "joining_confirmed", "active", "offboarding")
MIN_REJECTION_REASON = 5

# Set while mobility itself moves an internal Application (rejection / withdraw / hire): the pipeline guard lets
# those moves through.
_BYPASS: ContextVar[bool] = ContextVar("mobility_guard_bypass", default=False)


@contextlib.contextmanager
def guard_bypass() -> Iterator[None]:
    token = _BYPASS.set(True)
    try:
        yield
    finally:
        _BYPASS.reset(token)


def bypassing() -> bool:
    return _BYPASS.get()


def now() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    return today_for(TZ)


def not_found(what: str = "Internal application") -> NotFound:
    return NotFound(f"{what} not found")


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:  # pragma: no cover - tenant sessions only
        msg = "a tenant session is required"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def user_principal(ctx: RequestContext) -> Principal | None:
    p = ctx.principal
    return p if isinstance(p, Principal) and p.type == "user" else None


def has(ctx: RequestContext, permission: str) -> bool:
    p = ctx.principal
    return isinstance(p, Principal) and p.permissions.get(permission) is not None


def require(ctx: RequestContext, permission: str) -> None:
    if not has(ctx, permission):
        raise forbidden(permission)


def require_user(ctx: RequestContext) -> Principal:
    p = user_principal(ctx)
    if p is None:
        raise Forbidden("Internal users only", code="forbidden")
    return p


def system_ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


async def settings_row(db: AsyncSession) -> MobilitySettings:
    """The tenant's settings row (created with the defaults on first use)."""
    tid = tenant_of(db)
    row = await db.get(MobilitySettings, tid)
    if row is None:
        row = MobilitySettings(tenant_id=tid)
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row
