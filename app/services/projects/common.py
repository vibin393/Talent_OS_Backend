"""Shared helpers of the ``projects`` domain (Epic 45): tenant / clock / errors / scoping / timeline."""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.fiscal import local_date
from app.core.permissions import Principal, scope_condition
from app.models.projects import Project

ENTITY = "project"
DEFAULT_TZ = "Asia/Kolkata"
READ = "project:read"
WRITE = "project:write"


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "projects needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def now() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    return local_date(now(), DEFAULT_TZ)


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    if getattr(principal, "type", None) == "user":
        return getattr(principal, "id", None)
    return None


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, field: str | None = None, **details: Any) -> ValidationFailed:
    if field is not None:
        details = {**details, "fields": [{"field": field, "message": message}]}
    return ValidationFailed(message, code=code, details=details or None)


def _user_principal(ctx: RequestContext) -> Principal | None:
    principal = ctx.principal
    if isinstance(principal, Principal) and principal.type == "user":
        return principal
    return None


def scoped[S: Select[Any]](ctx: RequestContext, stmt: S, permission: str = READ) -> S:
    """Narrow a ``Project`` query to the caller's data scope (``owner_id`` / ``business_unit_id``). Internal
    contexts without a user principal (beat, event handlers, API keys holding the key) are not narrowed."""
    principal = _user_principal(ctx)
    if principal is None:
        return stmt
    cond = scope_condition(principal, permission, Project.owner_id, Project.business_unit_id)
    return stmt if cond is None else stmt.where(cond)


async def get_project(
    ctx: RequestContext,
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    permission: str = READ,
    lock: bool = False,
) -> Project:
    """Return the project if the caller may see it under ``permission`` (404 otherwise, also cross-tenant)."""
    stmt = scoped(ctx, select(Project).where(Project.id == project_id), permission)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Project not found")
    return row


async def timeline(
    ctx: RequestContext,
    db: AsyncSession,
    project_id: uuid.UUID,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    from app.services import activity_service

    await activity_service.add_system_entry(ctx, db, ENTITY, project_id, body[:500], data)


def can_view_rates(ctx: RequestContext) -> bool:
    principal = ctx.principal
    if not isinstance(principal, Principal):
        return True  # system contexts
    from app.core.permissions import COMPENSATION_VIEW

    return COMPENSATION_VIEW in principal.permissions
