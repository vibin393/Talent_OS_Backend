# ruff: noqa: E501, D401
"""Shared helpers of the ``workforce`` domain (Epic 44): tenant / clock / errors / scoping / loaders."""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.permissions import COMPENSATION_VIEW, Principal, scope_condition
from app.models.workforce import WorkforcePlan, WorkforcePlanLine, WorkforceSettings

READ = "workforce_plan:read"
WRITE = "workforce_plan:write"
APPROVE = "workforce_plan:approve"
APPROVAL_SUBJECT = "workforce_plan"
ENTITY = "workforce_plan"
# Plan statuses whose current-revision lines may be edited.
EDITABLE = ("draft", "budgeted", "revision_draft")


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "workforce needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def now() -> datetime:
    return datetime.now(UTC)


def system_ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    if isinstance(principal, Principal) and principal.type == "user":
        return principal.id
    return None


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, field: str | None = None, **details: Any) -> ValidationFailed:
    if field is not None:
        details = {**details, "fields": [{"field": field, "message": message}]}
    return ValidationFailed(message, code=code, details=details or None)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def add_months(d: date, months: int) -> date:
    """First-of-month arithmetic (callers pass month starts) -- day is clamped to the target month."""
    import calendar

    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    last = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(d.day, last))


def months_between(a: date, b: date) -> int:
    """Whole months from ``a``'s month to ``b``'s month (``b`` later -> positive)."""
    return (b.year - a.year) * 12 + (b.month - a.month)


def _user_principal(ctx: RequestContext) -> Principal | None:
    principal = ctx.principal
    if isinstance(principal, Principal) and principal.type == "user":
        return principal
    return None


def scoped[S: Select[Any]](ctx: RequestContext, stmt: S, permission: str = READ) -> S:
    """Narrow a ``WorkforcePlan`` query to the caller's data scope (owner / team / BU / tenant). Contexts without
    a user principal (event handlers, beat, the Client Portal path which uses its own module) are not narrowed."""
    principal = _user_principal(ctx)
    if principal is None:
        return stmt
    cond = scope_condition(principal, permission, WorkforcePlan.owner_id, WorkforcePlan.business_unit_id)
    return stmt if cond is None else stmt.where(cond)


async def get_plan(
    ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, *, permission: str = READ, lock: bool = False
) -> WorkforcePlan:
    """The plan if the caller may see it under ``permission`` (404 otherwise, also cross-tenant)."""
    stmt = scoped(ctx, select(WorkforcePlan).where(WorkforcePlan.id == plan_id), permission)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Workforce plan not found")
    return row


def can_view_money(ctx: RequestContext) -> bool:
    principal = ctx.principal
    if not isinstance(principal, Principal):
        return True  # system contexts
    return COMPENSATION_VIEW in principal.permissions


async def lines_of(
    db: AsyncSession, plan_id: uuid.UUID, revision: int, *, include_removed: bool = True
) -> list[WorkforcePlanLine]:
    stmt = select(WorkforcePlanLine).where(
        WorkforcePlanLine.workforce_plan_id == plan_id, WorkforcePlanLine.revision == revision
    )
    if not include_removed:
        stmt = stmt.where(WorkforcePlanLine.change_kind != "removed")
    stmt = stmt.order_by(
        WorkforcePlanLine.period_start, WorkforcePlanLine.role_title, WorkforcePlanLine.created_at
    )
    return list((await db.execute(stmt)).scalars())


async def effective_lines(db: AsyncSession, plan: WorkforcePlan) -> list[WorkforcePlanLine]:
    """Lines of the approved revision (what slots, conversion and actuals use); empty before the first approval."""
    if plan.approved_revision is None:
        return []
    return await lines_of(db, plan.id, plan.approved_revision, include_removed=False)


async def effective_line_for(
    db: AsyncSession, plan: WorkforcePlan, lineage_id: uuid.UUID
) -> WorkforcePlanLine | None:
    if plan.approved_revision is None:
        return None
    return (
        await db.execute(
            select(WorkforcePlanLine).where(
                WorkforcePlanLine.workforce_plan_id == plan.id,
                WorkforcePlanLine.revision == plan.approved_revision,
                WorkforcePlanLine.lineage_id == lineage_id,
            )
        )
    ).scalar_one_or_none()


async def settings_row(db: AsyncSession) -> WorkforceSettings:
    row = await db.get(WorkforceSettings, tenant_of(db))
    if row is None:
        row = WorkforceSettings(tenant_id=tenant_of(db))
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


async def tenant_currency(db: AsyncSession) -> str:
    from app.models.identity import Tenant

    value = (await db.execute(select(Tenant.currency).where(Tenant.id == tenant_of(db)))).scalar_one_or_none()
    return (value or "INR").upper()


def mark_picked_up(ctx: RequestContext, plan: WorkforcePlan) -> None:
    """An internal User touched a Client forecast: the Client can no longer edit or withdraw it."""
    if plan.origin == "client_forecast" and plan.picked_up_at is None and user_id(ctx) is not None:
        plan.picked_up_at = now()
