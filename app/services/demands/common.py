"""Shared plumbing of the demands service: principal / actor helpers, row visibility, settings row and the
activity timeline adapter."""

import contextlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.permissions import Principal, check, forbidden, scope_condition
from app.models.demands import Demand, DemandAssignment, DemandSettings
from app.services.demands.transitions import ActorKind

READ = "demand:read"
CREATE = "demand:create"
UPDATE = "demand:update"
DELETE = "demand:delete"
ASSIGN = "demand:assign"


def now() -> datetime:
    return datetime.now(UTC)


def principal_of(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    # Epic 67: the Public API (app / api_key) creates draft demands; ★ transitions refuse those actor kinds.
    if not isinstance(principal, Principal) or principal.type not in ("user", "app", "api_key") or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


def actor_kind_of(ctx: RequestContext) -> ActorKind:
    if getattr(ctx, "actor_kind", None) == "agent":  # Epic 53: an agent never passes as ``system``
        return "agent"
    kind = getattr(ctx.principal, "type", None)
    if kind == "app":  # Epic 66 (AD-75): an App never passes as ``system`` (no system transitions, FR-341)
        return "app"
    return "user" if kind == "user" else "system"


def not_found() -> NotFound:
    return NotFound("Demand not found")


@contextlib.contextmanager
def acting_as(db: AsyncSession, actor_type: str, actor_id: uuid.UUID | None = None) -> Iterator[None]:
    """Attribute audit rows flushed inside the block to ``actor_type`` (e.g. ``system`` hooks, ``ai`` intake).
    The caller must flush inside the block."""
    info = db.sync_session.info
    previous = info.get(audit.ACTOR_INFO_KEY)
    audit.set_actor(db, actor_type, actor_id)
    try:
        yield
    finally:
        if previous is None:
            info.pop(audit.ACTOR_INFO_KEY, None)
        else:
            info[audit.ACTOR_INFO_KEY] = previous


# ================================================================ visibility
def assigned_condition(principal: Principal) -> ColumnElement[bool]:
    """Demand is assigned to the principal directly or through one of their teams (active rows)."""
    me = principal.acting_user_id if principal.type == "app" else principal.id  # Epic 67: delegated App = its user
    who = [DemandAssignment.user_id == me]
    if principal.team_ids:
        who.append(DemandAssignment.team_id.in_(principal.team_ids))
    sub = select(DemandAssignment.demand_id).where(DemandAssignment.removed_at.is_(None), or_(*who))
    return Demand.id.in_(sub)


def access_condition(principal: Principal, permission: str) -> ColumnElement[bool] | None:
    """Scope rows for ``permission`` OR assignee rows (when the permission is held at all)."""
    if permission not in principal.permissions:
        return scope_condition(principal, permission, Demand.owner_id, Demand.business_unit_id)
    scoped = scope_condition(principal, permission, Demand.owner_id, Demand.business_unit_id)
    if scoped is None:
        return None
    return or_(scoped, assigned_condition(principal))


async def get_visible(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, *, for_update: bool = False
) -> Demand:
    principal = principal_of(ctx)
    stmt = select(Demand).where(Demand.id == demand_id)
    cond = access_condition(principal, READ)
    if cond is not None:
        stmt = stmt.where(cond)
    if for_update:
        stmt = stmt.with_for_update(of=Demand)
    demand = (await db.execute(stmt)).scalar_one_or_none()
    if demand is None:
        raise not_found()
    return demand


async def ensure_can(ctx: RequestContext, db: AsyncSession, demand: Demand, permission: str) -> None:
    """403 unless the principal holds ``permission`` with a scope (or assignment) covering ``demand``."""
    principal = principal_of(ctx)
    check(principal, permission)
    cond = access_condition(principal, permission)
    if cond is None:
        return
    hit = (await db.execute(select(Demand.id).where(and_(Demand.id == demand.id, cond)))).first()
    if hit is None:
        raise forbidden(permission)


async def get_for(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, permission: str, *, for_update: bool = True
) -> Demand:
    demand = await get_visible(ctx, db, demand_id, for_update=for_update)
    if permission != READ:
        await ensure_can(ctx, db, demand, permission)
    return demand


# ================================================================ settings row
async def demand_settings(ctx: RequestContext, db: AsyncSession, *, create: bool = True) -> DemandSettings:
    row = (await db.execute(select(DemandSettings))).scalar_one_or_none()
    if row is None:
        row = DemandSettings(tenant_id=ctx.tenant_id or tenant_of(db))
        if create:
            db.add(row)
            await db.flush()
            await db.refresh(row)
        else:
            row.req_prefix = "REQ"
    return row


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "demands service needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


# ================================================================ activity timeline
async def timeline(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, body: str, **data: Any
) -> None:
    """System entry on the demand timeline (ids / codes only, never PII)."""
    try:
        from app.services import activity_service
    except ImportError:  # pragma: no cover - Epic 2 absent
        logger.debug("activity service unavailable; demand timeline entry skipped")
        return
    await activity_service.add_system_entry(ctx, db, "demand", demand_id, body, data or None)
