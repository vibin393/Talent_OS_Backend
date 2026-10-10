"""Thin service-layer facade over the identity module for Epic 12 (AD-1).

Identity lives in ``app/models/identity.py`` / ``app/services/auth_service.py`` et al. Privacy, platform and
settings services reach tenants, users, roles and sessions only through these functions.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.permissions import TENANT_ADMIN, effective_role_grants
from app.models.identity import Role, Tenant, User, UserRole
from app.models.identity import Session as AuthSession


def _now() -> datetime:
    return datetime.now(UTC)


async def tenant_regional(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        return {}
    return {"timezone": tenant.timezone, "currency": tenant.currency}


async def set_tenant_regional(
    db: AsyncSession, tenant_id: uuid.UUID, *, timezone: str | None, currency: str | None
) -> list[str]:
    """Write the tenant's timezone/currency (platform table) with an explicit audit entry."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        return []
    changes: dict[str, dict[str, str]] = {}
    if timezone is not None and timezone != tenant.timezone:
        changes["timezone"] = {"old": tenant.timezone, "new": timezone}
        tenant.timezone = timezone
    if currency is not None and currency != tenant.currency:
        changes["currency"] = {"old": tenant.currency, "new": currency}
        tenant.currency = currency
    if changes:
        await db.flush()
        audit.record("tenant.regional_updated", "tenant", tenant_id, session=db, changes=changes)
    return sorted(changes)


async def users_with_permission(db: AsyncSession, permission: str) -> list[dict[str, Any]]:
    """Active users of the session's tenant holding ``permission`` (Tenant Admins always do)."""
    rows = (
        await db.execute(
            select(User.id, User.email, User.name, Role.key, Role.is_system, Role.permissions)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(User.is_active.is_(True))
        )
    ).all()
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for uid, email, name, key, is_system, perms in rows:
        grants = effective_role_grants(key, is_system, perms or {})
        if key == TENANT_ADMIN or permission in grants:
            out[uid] = {"id": uid, "email": email, "name": name}
    return list(out.values())


async def tenant_admins(db: AsyncSession) -> list[dict[str, Any]]:
    return await users_with_permission(db, "__tenant_admin_only__")


async def is_tenant_admin(db: AsyncSession, user_id: uuid.UUID) -> bool:
    stmt = (
        select(Role.id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, Role.key == TENANT_ADMIN)
    )
    return (await db.execute(stmt)).first() is not None


async def user_permissions(db: AsyncSession, user_id: uuid.UUID) -> dict[str, str]:
    from app.services import access_service as access

    loaded = await access.load_access(db, user_id)
    return {k: v.value for k, v in loaded.permissions.items()}


# ================================================================ tenants (platform table)
async def get_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    return await db.get(Tenant, tenant_id)


async def list_tenants(db: AsyncSession, statuses: tuple[str, ...] | None = None) -> list[Tenant]:
    stmt = select(Tenant)
    if statuses:
        stmt = stmt.where(Tenant.status.in_(statuses))
    return list((await db.execute(stmt.order_by(Tenant.created_at))).scalars())


async def tenant_status(db: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    return (await db.execute(select(Tenant.status).where(Tenant.id == tenant_id))).scalar_one_or_none()


async def revoke_tenant_sessions(
    db: AsyncSession, tenant_id: uuid.UUID, principal_types: tuple[str, ...] = ("user", "candidate", "support")
) -> int:
    result = await db.execute(
        update(AuthSession)
        .where(
            AuthSession.tenant_id == tenant_id,
            AuthSession.principal_type.in_(principal_types),
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


async def revoke_grant_sessions(db: AsyncSession, grant_id: uuid.UUID) -> int:
    result = await db.execute(
        update(AuthSession)
        .where(AuthSession.support_grant_id == grant_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
