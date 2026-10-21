"""Resolved access of tenant users (story 1.5): default-role seeding, role/team/BU assignment and the
permission map that goes on the ``Principal``.

Internal to the identity module; shared by ``service`` (sign-in, invitations, provisioning) and ``rbac``
(administration) so neither imports the other in a cycle. Every function takes a tenant-scoped session.
"""

import dataclasses
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    DEFAULT_ROLES,
    TENANT_ADMIN,
    Principal,
    Scope,
    effective_role_grants,
    merge_grants,
)
from app.models.identity import BusinessUnit, Invitation, Role, Team, TeamMember, Tenant, User, UserRole


@dataclasses.dataclass(frozen=True, slots=True)
class Access:
    role_ids: frozenset[uuid.UUID]
    role_keys: tuple[str, ...]  # sorted
    permissions: dict[str, Scope]
    team_ids: frozenset[uuid.UUID]


async def seed_default_roles(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Insert the default (system) roles missing for ``tenant_id``; idempotent. Returns key -> id."""
    await db.execute(
        pg_insert(Role)
        .values(
            [
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "key": template.key,
                    "name": template.name,
                    "description": template.description,
                    "is_system": True,
                    "permissions": {k: v.value for k, v in template.permissions.items()},
                }
                for template in DEFAULT_ROLES
            ]
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
    )
    keys = [t.key for t in DEFAULT_ROLES]
    rows = await db.execute(select(Role.key, Role.id).where(Role.key.in_(keys)))
    return dict(rows.tuples().all())


async def tenant_admin_role_id(db: AsyncSession, *, for_update: bool = False) -> uuid.UUID | None:
    stmt = select(Role.id).where(Role.key == TENANT_ADMIN)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def lock_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    """``SELECT ... FOR UPDATE`` the tenant row: serializes seat-consuming changes (invite, accept, ...)."""
    return (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    ).scalar_one_or_none()


async def active_user_count(db: AsyncSession) -> int:
    count = (
        await db.execute(select(func.count()).select_from(User).where(User.is_active.is_(True)))
    ).scalar_one()
    return int(count)


async def seats_used(db: AsyncSession) -> int:
    """Active users + pending (unaccepted, unrevoked, unexpired) invitations, in one statement."""
    active = select(func.count()).select_from(User).where(User.is_active.is_(True)).scalar_subquery()
    pending = (
        select(func.count())
        .select_from(Invitation)
        .where(
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > datetime.now(UTC),
        )
        .scalar_subquery()
    )
    return int((await db.execute(select(active + pending))).scalar_one())


async def module_caps_for_user(db: AsyncSession, user_id: uuid.UUID) -> dict[str, list[str]]:
    """Unified RBAC: the user's effective module capabilities — the union across their assigned roles
    (the Tenant Admin role confers every cap). Shape ``{app: [cap, ...]}``; apps with no granted cap are
    omitted. Imported lazily so ``core`` / ``identity`` keep no dependency on the module-RBAC service."""
    from app.services import module_rbac

    roles = (
        await db.execute(
            select(Role.key, Role.is_system, Role.module_caps)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
        )
    ).all()
    return module_rbac.effective_module_caps((r.key, r.is_system, r.module_caps) for r in roles)


async def load_access(db: AsyncSession, user_id: uuid.UUID) -> Access:
    roles = (
        await db.execute(
            select(Role.id, Role.key, Role.is_system, Role.permissions)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
        )
    ).all()
    team_ids = (await db.execute(select(TeamMember.team_id).where(TeamMember.user_id == user_id))).scalars()
    return Access(
        role_ids=frozenset(r.id for r in roles),
        role_keys=tuple(sorted(r.key for r in roles)),
        permissions=merge_grants(effective_role_grants(r.key, r.is_system, r.permissions) for r in roles),
        team_ids=frozenset(team_ids),
    )


def user_principal(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID | None,
    business_unit_id: uuid.UUID | None,
    access: Access,
) -> Principal:
    return Principal(
        type="user",
        id=user_id,
        session_id=session_id,
        tenant_id=tenant_id,
        role_ids=access.role_ids,
        permissions=dict(access.permissions),
        team_ids=access.team_ids,
        business_unit_id=business_unit_id,
    )


# ---------------------------------------------------------------- existence checks (RLS-scoped)
async def existing_role_ids(db: AsyncSession, ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    wanted = set(ids)
    if not wanted:
        return set()
    return set((await db.execute(select(Role.id).where(Role.id.in_(wanted)))).scalars())


async def existing_team_ids(db: AsyncSession, ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    wanted = set(ids)
    if not wanted:
        return set()
    return set((await db.execute(select(Team.id).where(Team.id.in_(wanted)))).scalars())


async def business_unit_exists(db: AsyncSession, business_unit_id: uuid.UUID) -> bool:
    return (await db.get(BusinessUnit, business_unit_id)) is not None


# ---------------------------------------------------------------- assignment
async def set_user_roles(
    db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, role_ids: Iterable[uuid.UUID]
) -> None:
    """Replace the user's roles with ``role_ids`` (callers validated them)."""
    wanted = set(role_ids)
    rows = (await db.execute(select(UserRole).where(UserRole.user_id == user_id))).scalars().all()
    current = {r.role_id for r in rows}
    for row in rows:  # ORM deletes, so the audit listener records each removal
        if row.role_id not in wanted:
            await db.delete(row)
    for role_id in wanted - current:
        db.add(UserRole(tenant_id=tenant_id, user_id=user_id, role_id=role_id))
    await db.flush()


async def set_user_teams(
    db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, team_ids: Iterable[uuid.UUID]
) -> None:
    """Replace the user's team memberships (kept teams keep their ``is_lead`` flag)."""
    wanted = set(team_ids)
    rows = (await db.execute(select(TeamMember).where(TeamMember.user_id == user_id))).scalars().all()
    current = {r.team_id for r in rows}
    for row in rows:  # ORM deletes, so the audit listener records each removal
        if row.team_id not in wanted:
            await db.delete(row)
    for team_id in wanted - current:
        db.add(TeamMember(tenant_id=tenant_id, team_id=team_id, user_id=user_id))
    await db.flush()
