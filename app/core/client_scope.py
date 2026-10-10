"""Client User visibility restrictions (AD-26, FR-79).

A Client User marked ``restricted`` sees only the demands / placements listed in ``client_user_scopes`` (plus
placements whose demand is a scoped demand). Internal principals and unrestricted Client Users get ``None``
(no restriction beyond org RLS). Reads through ``sqlalchemy.table()`` constructs so ``core`` never imports a
domain module.
"""

import uuid

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext

_client_users = table("client_users", column("id"), column("restricted"))
_scopes = table("client_user_scopes", column("client_user_id"), column("subject_kind"), column("subject_id"))
_placements = table("placements", column("id"), column("demand_id"))


async def _restricted_user(ctx: RequestContext, db: AsyncSession) -> uuid.UUID | None:
    principal = ctx.principal
    if getattr(principal, "type", None) != "client_user":
        return None
    user_id = getattr(principal, "id", None)
    restricted = (
        await db.execute(select(_client_users.c.restricted).where(_client_users.c.id == user_id))
    ).scalar_one_or_none()
    # A Client User row that cannot be read is treated as fully restricted (fail-closed).
    return user_id if restricted is None or bool(restricted) else None


async def _scoped(db: AsyncSession, client_user_id: uuid.UUID, kind: str) -> set[uuid.UUID]:
    rows = await db.execute(
        select(_scopes.c.subject_id).where(
            _scopes.c.client_user_id == client_user_id, _scopes.c.subject_kind == kind
        )
    )
    return set(rows.scalars())


async def visible_demand_ids(ctx: RequestContext, db: AsyncSession) -> frozenset[uuid.UUID] | None:
    user_id = await _restricted_user(ctx, db)
    if user_id is None:
        return None
    return frozenset(await _scoped(db, user_id, "demand"))


async def visible_placement_ids(ctx: RequestContext, db: AsyncSession) -> frozenset[uuid.UUID] | None:
    user_id = await _restricted_user(ctx, db)
    if user_id is None:
        return None
    placements = await _scoped(db, user_id, "placement")
    demands = await _scoped(db, user_id, "demand")
    if demands:
        rows = await db.execute(select(_placements.c.id).where(_placements.c.demand_id.in_(demands)))
        placements |= set(rows.scalars())
    return frozenset(placements)
