# ruff: file-ignore[line-too-long]
"""Public directory reads (Epic 67, spec A4): Clients and Contacts (``clients_service`` scope: the client owner under
``client:read`` / ``contact:read``), Users (directory fields only; email is contact data), Talent Pools
(``talent_pools.common.visible``) with members, and the platform Skills catalogue."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import scope_condition
from app.models.candidates import Skill
from app.models.clients import Client, Contact
from app.models.identity import TeamMember, User
from app.models.talent_pools import TalentPool, TalentPoolMember
from app.services.public_api._core import Order, base, ctx_principal, expose, finish_all, one, page
from app.services.talent_pools.common import visible as pool_visible


# ================================================================ clients / contacts
def _clients(permission: str = "client:read") -> Any:
    _, principal = ctx_principal()
    stmt = select(Client).where(Client.archived_at.is_(None))
    cond = scope_condition(principal, permission, Client.owner_id)
    return stmt if cond is None else stmt.where(cond)


async def _build_clients(rows: Sequence[Client]) -> list[dict[str, Any]]:  # NOSONAR
    _, principal = ctx_principal()
    return finish_all(principal, [
        {**base(c, "client"), "name": c.name, "industry": c.industry, "website": c.website, "status": c.status, "owner_id": c.owner_id}
        for c in rows
    ])  # fmt: skip


async def list_clients(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _clients(), sort_col=Client.updated_at, id_col=Client.id, limit=limit, cursor=cursor, order=order, build=_build_clients)


async def get_client(db: AsyncSession, client_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_clients([await one(db, _clients().where(Client.id == client_id))]))[0]


def _contacts() -> Any:
    visible_clients = _clients("contact:read").with_only_columns(Client.id)
    return select(Contact).where(Contact.archived_at.is_(None), Contact.client_id.in_(visible_clients))


async def _build_contacts(rows: Sequence[Contact]) -> list[dict[str, Any]]:  # NOSONAR
    _, principal = ctx_principal()
    return finish_all(principal, [
        {**base(c, "contact"), "client_id": c.client_id, "name": c.name, "title": c.title, "email": c.email, "phone": c.phone}
        for c in rows
    ])  # fmt: skip


async def list_contacts(db: AsyncSession, *, limit: int, cursor: str | None, order: Order, client_id: uuid.UUID | None = None) -> dict[str, Any]:
    stmt = _contacts()
    if client_id is not None:
        stmt = stmt.where(Contact.client_id == client_id)
    return await page(db, stmt, sort_col=Contact.updated_at, id_col=Contact.id, limit=limit, cursor=cursor, order=order, build=_build_contacts)


async def get_contact(db: AsyncSession, contact_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_contacts([await one(db, _contacts().where(Contact.id == contact_id))]))[0]


# ================================================================ users
async def _build_users(db: AsyncSession, rows: Sequence[User]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    teams: dict[uuid.UUID, list[uuid.UUID]] = {}
    if rows:
        for uid, tid in (await db.execute(select(TeamMember.user_id, TeamMember.team_id).where(TeamMember.user_id.in_([r.id for r in rows])))).all():
            teams.setdefault(uid, []).append(tid)
    return finish_all(principal, [
        {**base(u, "user"), "display_name": u.name, "email": u.email, "title": None, "team_ids": sorted(teams.get(u.id, []), key=str),
         "business_unit_id": u.business_unit_id, "active": u.is_active}
        for u in rows
    ])  # fmt: skip


async def list_users(db: AsyncSession, *, limit: int, cursor: str | None, order: Order, active: bool | None = None) -> dict[str, Any]:
    stmt = select(User)
    if active is not None:
        stmt = stmt.where(User.is_active.is_(active))
    return await page(db, stmt, sort_col=User.updated_at, id_col=User.id, limit=limit, cursor=cursor, order=order, build=lambda r: _build_users(db, r))


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_users(db, [await one(db, select(User).where(User.id == user_id))]))[0]


# ================================================================ pools / skills
def _pools() -> Any:
    _, principal = ctx_principal()
    return select(TalentPool).where(TalentPool.archived_at.is_(None), pool_visible(principal))


async def _build_pools(rows: Sequence[TalentPool]) -> list[dict[str, Any]]:  # NOSONAR
    _, principal = ctx_principal()
    return finish_all(principal, [
        {**base(p, "pool"), "name": p.name, "kind": p.kind, "description": p.description, "member_count": p.member_count, "status": p.status}
        for p in rows
    ])  # fmt: skip


async def list_pools(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _pools(), sort_col=TalentPool.updated_at, id_col=TalentPool.id, limit=limit, cursor=cursor, order=order, build=_build_pools)


async def get_pool(db: AsyncSession, pool_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_pools([await one(db, _pools().where(TalentPool.id == pool_id))]))[0]


async def pool_members(db: AsyncSession, pool_id: uuid.UUID, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    _, principal = ctx_principal()
    await one(db, _pools().where(TalentPool.id == pool_id))
    stmt = select(TalentPoolMember).where(TalentPoolMember.pool_id == pool_id, TalentPoolMember.removed_at.is_(None))

    async def build(rows: Sequence[TalentPoolMember]) -> list[dict[str, Any]]:
        await expose(principal, "candidate", [r.candidate_id for r in rows])
        return [{"candidate_id": r.candidate_id, "added_at": r.added_at} for r in rows]

    return await page(db, stmt, sort_col=TalentPoolMember.added_at, id_col=TalentPoolMember.id, limit=limit, cursor=cursor, order=order, build=build)


async def list_skills(db: AsyncSession, *, limit: int, cursor: str | None, order: Order, q: str | None = None) -> dict[str, Any]:
    _, principal = ctx_principal()
    stmt = select(Skill).where(Skill.is_active.is_(True))
    if q and q.strip():
        stmt = stmt.where(Skill.name.ilike(f"%{q.strip().replace('%', '').replace('_', '')}%"))

    async def build(rows: Sequence[Skill]) -> list[dict[str, Any]]:  # NOSONAR
        return finish_all(principal, [{**base(s, "skill"), "name": s.name, "slug": s.slug, "category": s.category} for s in rows])

    return await page(db, stmt, sort_col=Skill.updated_at, id_col=Skill.id, limit=limit, cursor=cursor, order=order, build=build)


__all__ = [
    "get_client", "get_contact", "get_pool", "get_user", "list_clients", "list_contacts", "list_pools", "list_skills",
    "list_users", "pool_members",
]  # fmt: skip
