"""Shared helpers of the ``immigration`` domain (Epic 46): permissions, errors, data scope, actors, clock."""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.fiscal import today_for
from app.core.permissions import Principal, forbidden, scope_condition

IMM_READ = "immigration:read"
IMM_MANAGE = "immigration:manage"
IMM_CONFIGURE = "immigration:configure"
IDENTITY_VIEW = "identity:view"

OWNER_KIND = "immigration_case"  # P2 documents owner kind, tasks entity type, files access checker
DOCUMENT_ACCESS = "hr"
RETENTION_CLASS = "immigration"
LINK_PURPOSE = "immigration_upload"
CONSENT_PURPOSE = "immigration"
TZ = "Asia/Kolkata"
OPEN_STATUSES = ("open", "in_stage")
OPEN_PLACEMENT_STATUSES = ("pre_joining", "joining_confirmed", "active", "offboarding")


def now() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    return today_for(TZ)


def not_found(what: str = "Immigration case") -> NotFound:
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
    if isinstance(p, Principal) and p.type == "user":
        return p
    return None


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    p = user_principal(ctx)
    return p.id if p else None


def has(ctx: RequestContext, permission: str) -> bool:
    p = ctx.principal
    return isinstance(p, Principal) and p.permissions.get(permission) is not None


def require(ctx: RequestContext, permission: str) -> None:
    if ctx.principal is None:  # system / task context
        return
    if not has(ctx, permission):
        raise forbidden(permission)


def actor_json(ctx: RequestContext) -> dict[str, Any]:
    return actor_from_ctx(ctx).to_json()


def actor_of(ctx: RequestContext) -> ActorRef:
    return actor_from_ctx(ctx)


def scoped(ctx: RequestContext, stmt: Select[Any], permission: str = IMM_READ) -> Select[Any]:
    """Apply the Case data scope (owner / BU) for user principals; system contexts see everything."""
    from app.models.immigration import ImmigrationCase

    p = ctx.principal
    if not isinstance(p, Principal):
        return stmt
    cond = scope_condition(p, permission, ImmigrationCase.owner_user_id, ImmigrationCase.business_unit_id)
    return stmt if cond is None else stmt.where(cond)


def system_ctx(db: AsyncSession) -> RequestContext:
    return RequestContext(tenant_id=tenant_of(db), actor_kind="system")
