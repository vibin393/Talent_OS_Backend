"""Thin read helpers over users, teams and roles for other domains (Epic 4: demands and approvals).

Read-only; every function except ``active_tenant_ids`` / ``tenant_timezone`` takes a tenant-scoped session
(RLS confines results to that tenant).
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import platform_session
from app.core.permissions import Scope, effective_role_grants
from app.models.identity import Role, Team, TeamMember, Tenant, User, UserRole


async def team_member_ids(db: AsyncSession, team_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    ids = set(team_ids)
    if not ids:
        return set()
    rows = await db.execute(
        select(TeamMember.user_id)
        .join(User, User.id == TeamMember.user_id)
        .where(TeamMember.team_id.in_(ids), User.is_active.is_(True))
    )
    return set(rows.scalars())


async def user_team_ids(db: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await db.execute(select(TeamMember.team_id).where(TeamMember.user_id == user_id))
    return set(rows.scalars())


async def user_ids_with_role(db: AsyncSession, role_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await db.execute(
        select(UserRole.user_id)
        .join(User, User.id == UserRole.user_id)
        .where(UserRole.role_id == role_id, User.is_active.is_(True))
    )
    return set(rows.scalars())


async def bu_manager_ids(db: AsyncSession, bu_id: uuid.UUID | None) -> set[uuid.UUID]:
    """Active users holding ``demand:assign`` at tenant scope, or at bu scope within ``bu_id``."""
    rows = await db.execute(
        select(User.id, User.business_unit_id, Role.key, Role.is_system, Role.permissions)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(User.is_active.is_(True))
    )
    out: set[uuid.UUID] = set()
    for user_id, user_bu, key, is_system, perms in rows.all():
        raw = effective_role_grants(key, is_system, perms).get("demand:assign")
        if raw == Scope.TENANT.value or (raw == Scope.BU.value and bu_id is not None and user_bu == bu_id):
            out.add(user_id)
    return out


async def active_user_ids(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    ids = set(user_ids)
    if not ids:
        return set()
    rows = await db.execute(select(User.id).where(User.id.in_(ids), User.is_active.is_(True)))
    return set(rows.scalars())


async def existing_team_ids(db: AsyncSession, team_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    ids = set(team_ids)
    if not ids:
        return set()
    return set((await db.execute(select(Team.id).where(Team.id.in_(ids)))).scalars())


async def existing_role_ids(db: AsyncSession, role_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    ids = set(role_ids)
    if not ids:
        return set()
    return set((await db.execute(select(Role.id).where(Role.id.in_(ids)))).scalars())


async def user_names(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = set(user_ids)
    if not ids:
        return {}
    return dict((await db.execute(select(User.id, User.name).where(User.id.in_(ids)))).tuples().all())


async def team_names(db: AsyncSession, team_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = set(team_ids)
    if not ids:
        return {}
    return dict((await db.execute(select(Team.id, Team.name).where(Team.id.in_(ids)))).tuples().all())


async def role_names(db: AsyncSession, role_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = set(role_ids)
    if not ids:
        return {}
    return dict((await db.execute(select(Role.id, Role.name).where(Role.id.in_(ids)))).tuples().all())


async def tenant_timezone(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """``tenants`` is a platform table (no RLS), readable from a tenant session."""
    tz = (await db.execute(select(Tenant.timezone).where(Tenant.id == tenant_id))).scalar_one_or_none()
    return tz or "UTC"


async def active_tenant_ids() -> list[uuid.UUID]:
    async with platform_session() as db:
        rows = await db.execute(select(Tenant.id).where(Tenant.status == "active"))
        return list(rows.scalars())


async def role_id_by_key(db: AsyncSession, key: str) -> uuid.UUID | None:
    """Epic 13 (R19): the tenant role with ``key`` (e.g. ``finance_admin``), or None."""
    return (await db.execute(select(Role.id).where(Role.key == key))).scalar_one_or_none()
