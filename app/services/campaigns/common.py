"""Shared helpers of the ``campaigns`` domain (Epic 39)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, NotFound
from app.core.permissions import Principal

READ = "campaign:read"
WRITE = "campaign:write"
APPROVE = "campaign:approve"
SEND = "campaign:send"
SEQUENCE_WRITE = "sequence:write"
CONFIGURE = "marketing:configure"


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def principal_of(ctx: RequestContext) -> Principal:
    p = ctx.principal
    if not isinstance(p, Principal) or p.type not in ("user", "api_key", "app"):  # Epic 66: +app
        raise Forbidden("Internal users only")
    return p


def user_id_of(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return p.id if isinstance(p, Principal) and p.type == "user" else None


def system_ctx(
    tenant_id: uuid.UUID, actor_kind: str = "system", actor_id: uuid.UUID | None = None
) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind=actor_kind, actor_id=actor_id)


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details)


def not_found(what: str) -> NotFound:
    return NotFound(f"{what} not found")


def as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
