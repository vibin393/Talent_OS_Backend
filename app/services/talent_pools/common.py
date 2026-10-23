# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Shared rules of the ``talent_pools`` domain (Epic 38).

* **Pool visibility** (``visible``): owner, team Pools of the viewer's teams, tenant Pools, Pools explicitly
  shared with the viewer or a team of the viewer (``talent_pool_shares``, carried over from P1 lists);
  ``pool:manage`` sees every Pool (it edits and refreshes any Pool).
* **Member scope** (``member_scope``): every member read is filtered again by the viewer's ``candidate:read``
  scope on the candidate owner (merged / erased candidates are never listed). A system context (tasks) skips it.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, false, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.permissions import Principal, forbidden, scope_condition
from app.models.candidates import Candidate
from app.models.talent_pools import TalentPool, TalentPoolShare

READ = "pool:read"
WRITE = "pool:write"
MANAGE = "pool:manage"
SEGMENT_WRITE = "segment:write"
CANDIDATE_READ = "candidate:read"


def now() -> datetime:
    return datetime.now(UTC)


def principal_of(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type not in ("user", "api_key", "app"):  # Epic 66: +app
        raise NotFound("Pool not found")
    return principal


def is_system(ctx: RequestContext) -> bool:
    return ctx.principal is None


def shared_pool_ids(principal: Principal) -> Any:
    return select(TalentPoolShare.pool_id).where(
        or_(
            TalentPoolShare.user_id == principal.id,
            TalentPoolShare.team_id.in_(list(principal.team_ids)) if principal.team_ids else false(),
        )
    )


def visible(principal: Principal) -> ColumnElement[bool]:
    if principal.has(MANAGE):
        return true()
    return or_(
        TalentPool.owner_id == principal.id,
        TalentPool.visibility == "tenant",
        and_(
            TalentPool.visibility == "team",
            TalentPool.team_id.in_(list(principal.team_ids)) if principal.team_ids else false(),
        ),
        TalentPool.id.in_(shared_pool_ids(principal)),
    )


def can_edit(principal: Principal, pool: TalentPool) -> bool:
    """Owner with ``pool:write`` or ``pool:manage``."""
    return principal.has(MANAGE) or (pool.owner_id == principal.id and principal.has(WRITE))


async def is_shared_with(db: AsyncSession, principal: Principal, pool: TalentPool) -> bool:
    if pool.visibility == "team" and pool.team_id in principal.team_ids:
        return True
    found = (
        await db.execute(
            select(TalentPoolShare.id).where(
                TalentPoolShare.pool_id == pool.id, TalentPoolShare.pool_id.in_(shared_pool_ids(principal))
            )
        )
    ).first()
    return found is not None


async def can_write_members(db: AsyncSession, principal: Principal, pool: TalentPool) -> bool:
    """Owner / manager, or ``pool:write`` holders the Pool is shared with (P1 list rule: shared users add)."""
    if can_edit(principal, pool):
        return True
    return principal.has(WRITE) and await is_shared_with(db, principal, pool)


async def get_pool(
    ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID, *, for_update: bool = False
) -> TalentPool:
    """A Pool the caller may see (404 otherwise, incl. other tenants); system contexts see every Pool."""
    stmt = select(TalentPool).where(TalentPool.id == pool_id)
    if not is_system(ctx):
        stmt = stmt.where(visible(principal_of(ctx)))
    if for_update:
        stmt = stmt.with_for_update()
    pool = (await db.execute(stmt)).scalar_one_or_none()
    if pool is None:
        raise NotFound("Pool not found")
    return pool


def require_edit(principal: Principal, pool: TalentPool) -> None:
    if not can_edit(principal, pool):
        raise forbidden(WRITE)


def require_active(pool: TalentPool) -> None:
    if pool.status != "active":
        raise ConflictError("This pool is archived", code="pool_archived")


def member_scope(ctx: RequestContext) -> ColumnElement[bool]:
    """Condition on ``Candidate`` rows the viewer may read (plus the base merged / erased filter)."""
    base = and_(Candidate.merged_into_id.is_(None), Candidate.erased_at.is_(None))
    if is_system(ctx):
        return base
    principal = principal_of(ctx)
    cond = scope_condition(principal, CANDIDATE_READ, Candidate.owner_id)
    return base if cond is None else and_(base, cond)
