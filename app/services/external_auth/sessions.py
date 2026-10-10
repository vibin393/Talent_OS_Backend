"""Client User / Vendor User sessions (AD-10, AD-26).

Same mechanism as users: an opaque random token stored hashed in the platform ``sessions`` table, sent as the
httpOnly ``tos_client`` / ``tos_vendor`` cookie, plus the double-submit CSRF cookie. Idle limit
``EXTERNAL_SESSION_IDLE_MINUTES`` (30), absolute ``EXTERNAL_SESSION_ABSOLUTE_HOURS`` (12).

``resolve`` checks, in order: cookie, hashed token, revocation, idle / absolute expiry, tenant active,
principal status (and, for Vendor Users, the vendor's status ``active`` / ``onboarding``), then CSRF on
mutating requests, and only then touches ``last_seen_at``. A suspended / revoked principal or vendor gets 401
and every session of that principal is revoked. Sign-in flows are Epic 14; tests and Epic 14 call
``create_session``.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import update_context
from app.core.database import platform_session, tenant_session
from app.core.errors import Unauthorized
from app.core.permissions import Principal
from app.core.security import SESSION_COOKIES, clear_auth_cookies, csrf_token_for, verify_csrf
from app.core.settings import get_settings
from app.models.external_auth import ClientUser, VendorUser
from app.models.identity import Session as AuthSession
from app.models.vendors import Vendor

ExternalType = Literal["client_user", "vendor_user"]
PRINCIPAL_STATE_ATTR = "tos_external_principal"
LAST_SEEN_RESOLUTION = timedelta(seconds=60)
VENDOR_OK_STATUSES = frozenset({"active", "onboarding"})


def _now() -> datetime:
    return datetime.now(UTC)


def _idle() -> timedelta:
    return timedelta(minutes=get_settings().external_session_idle_minutes)


def _absolute() -> timedelta:
    return timedelta(hours=get_settings().external_session_absolute_hours)


def _rejected(principal_type: ExternalType) -> Unauthorized:
    exc = Unauthorized("Session expired or revoked", code="session_expired")
    cookie = SESSION_COOKIES[principal_type]
    exc.response_hooks.append(lambda response: clear_auth_cookies(response, cookie))
    return exc


async def create_session(
    db: AsyncSession,
    principal_type: ExternalType,
    principal_id: uuid.UUID,
    tenant_id: uuid.UUID,
    request: Request | None = None,
) -> tuple[str, str]:
    """Issue a session for an external principal; returns ``(token, csrf)`` (raw, never persisted)."""
    from app.services import auth_service

    issued = await auth_service.create_session(
        db,
        principal_type=principal_type,
        principal_id=principal_id,
        tenant_id=tenant_id,
        ip=request.client.host if request is not None and request.client is not None else None,
        user_agent=request.headers.get("user-agent") if request is not None else None,
        absolute_timeout=_absolute(),
    )
    return issued.token, csrf_token_for(issued.token)


async def revoke_all(db: AsyncSession, principal_type: ExternalType, principal_id: uuid.UUID) -> int:
    from app.services import auth_service

    return await auth_service.revoke_principal_sessions(db, principal_type, principal_id)


async def _principal_facts(
    db: AsyncSession, principal_type: ExternalType, principal_id: uuid.UUID
) -> tuple[uuid.UUID, frozenset[str]] | None:
    """``(org_id, portal_permissions)`` of a usable principal, else None."""
    if principal_type == "client_user":
        cu = (await db.execute(select(ClientUser).where(ClientUser.id == principal_id))).scalar_one_or_none()
        if cu is None or cu.status != "active":
            return None
        return cu.client_id, frozenset(cu.portal_permissions or ())
    vu = (await db.execute(select(VendorUser).where(VendorUser.id == principal_id))).scalar_one_or_none()
    if vu is None or vu.status != "active":
        return None
    vendor_status = (
        await db.execute(select(Vendor.status).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if vendor_status not in VENDOR_OK_STATUSES:
        return None
    return vu.vendor_id, frozenset(vu.portal_permissions or ())


async def resolve(request: Request, principal_type: ExternalType) -> Principal:
    cached = getattr(request.state, PRINCIPAL_STATE_ATTR, None)
    if isinstance(cached, Principal) and cached.type == principal_type:
        return cached
    from app.services import auth_service

    token = request.cookies.get(SESSION_COOKIES[principal_type])
    if not token:
        raise Unauthorized()
    at = _now()
    async with platform_session() as pdb:
        row = await auth_service.find_session(pdb, principal_type, token)
        live = (
            row is not None
            and row.tenant_id is not None
            and auth_service.session_is_live(row, at, _idle())
            and at - row.created_at <= _absolute()
        )
        session_id = row.id if row is not None else None
        tenant_id = row.tenant_id if row is not None else None
        principal_id = row.principal_id if row is not None else None
        last_seen = row.last_seen_at if row is not None else None
    if not live or session_id is None or tenant_id is None or principal_id is None:
        raise _rejected(principal_type)
    if await auth_service.active_tenant(tenant_id) is None:
        raise _rejected(principal_type)
    # Internal scope: the principal's own row decides; the request's data session is org-scoped.
    async with tenant_session(tenant_id) as db:
        facts = await _principal_facts(db, principal_type, principal_id)
        if facts is None:
            await revoke_all(db, principal_type, principal_id)
        else:
            verify_csrf(request)
            if last_seen is not None and at - last_seen >= LAST_SEEN_RESOLUTION:
                await db.execute(
                    update(AuthSession).where(AuthSession.id == session_id).values(last_seen_at=at)
                )
    if facts is None:
        raise _rejected(principal_type)
    org_id, portal_permissions = facts
    principal = Principal(
        type=principal_type,
        id=principal_id,
        session_id=session_id,
        tenant_id=tenant_id,
        org_id=org_id,
        portal_permissions=portal_permissions,
    )
    update_context(principal=principal, tenant_id=tenant_id)
    setattr(request.state, PRINCIPAL_STATE_ATTR, principal)
    return principal
