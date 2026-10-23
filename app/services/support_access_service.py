"""Support access (Epic 12 story 12.5, FR-69).

* ``support_access_grants`` is tenant-owned (RLS) and audited; the operator side reaches it through
  ``tenant_session(tenant_id)``.
* ``SUPPORT_TRANSITIONS`` (AD-4): pending -> approved/denied (tenant user), pending -> cancelled (operator),
  approved -> revoked (tenant user), pending/approved -> expired (system).
* ``expires_at = approved_at + min(requested_hours, SUPPORT_MAX_HOURS)``.
* A support session is a ``sessions`` row with ``principal_type='support'`` and ``support_grant_id``, sent as
  the ``tos_support`` cookie expiring at ``expires_at``. Every request re-checks the grant (no caching) and is
  read-only (``resolve_support_principal``); each one is pre-audited as ``support.request`` in its own
  transaction.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext, update_context
from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, Forbidden, NotFound, Unauthorized
from app.core.permissions import CATALOG, Principal, Scope
from app.core.security import hash_token
from app.core.settings import get_settings
from app.models.identity import PlatformOperator, Tenant
from app.models.identity import Session as AuthSession
from app.models.platform import SupportAccessGrant, SupportGrantLookup
from app.schemas.identity import AuthOut, SupportSessionInfo, TenantSummary, UserOut
from app.schemas.platform import SupportGrantOut, SupportRequestCreate, SupportSessionOut
from app.services import identity_bridge
from app.services.privacy.common import email_users

SUPPORT_COOKIE = "tos_support"
SUPPORT_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("pending", "approved"): frozenset({"user"}),
    ("pending", "denied"): frozenset({"user"}),
    ("pending", "cancelled"): frozenset({"platform"}),
    ("approved", "revoked"): frozenset({"user"}),
    ("pending", "expired"): frozenset({"system"}),
    ("approved", "expired"): frozenset({"system"}),
}
PENDING_TTL = timedelta(days=7)
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ALLOWED_WRITE_PATHS = frozenset({"/api/v1/auth/logout", "/api/v1/support/logout"})


def _now() -> datetime:
    return datetime.now(UTC)


def support_permissions() -> dict[str, Scope]:
    """Every ``*:read`` permission at tenant scope, never ``compensation:view`` nor any ``*:export``."""
    return {
        key: Scope.TENANT
        for key in CATALOG
        if key.endswith(":read") and key != "compensation:view" and not key.endswith(":export")
    }


def _out(row: SupportAccessGrant, operator_name: str | None = None) -> SupportGrantOut:
    out = SupportGrantOut.model_validate(row)
    out.operator_name = operator_name
    return out


async def _operator_names(ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    async with platform_session() as pdb:
        rows = await pdb.execute(
            select(PlatformOperator.id, PlatformOperator.name).where(PlatformOperator.id.in_(ids))
        )
        return dict(rows.tuples().all())


async def _transition(
    db: AsyncSession, row: SupportAccessGrant, to: str, actor: str, actor_id: uuid.UUID | None
) -> None:
    allowed = SUPPORT_TRANSITIONS.get((row.status, to))
    if allowed is None or actor not in allowed:
        raise ConflictError(
            f"Cannot move a {row.status} support request to {to}",
            code="invalid_transition",
            details={"from": row.status, "to": to},
        )
    now = _now()
    old = row.status
    row.status = to
    if to in ("approved", "denied"):
        row.decided_by, row.decided_at = actor_id, now
    if to == "approved":
        hours = min(row.requested_hours, get_settings().support_max_hours)
        row.approved_at = now
        row.expires_at = now + timedelta(hours=hours)
    if to == "revoked":
        row.revoked_by, row.revoked_at = actor_id, now
    await db.flush()
    audit.record(
        "support.transitioned", "support_access_grant", row.id, session=db, **{"from": old, "to": to}
    )
    if to in ("revoked", "expired", "denied", "cancelled"):
        await identity_bridge.revoke_grant_sessions(db, row.id)


def _require_operator(ctx: RequestContext) -> uuid.UUID:
    principal = ctx.principal
    if getattr(principal, "type", None) != "platform":
        raise Unauthorized()
    return principal.id  # type: ignore[no-any-return]


# ================================================================ operator side
async def request_access(
    ctx: RequestContext, tenant_id: uuid.UUID, data: SupportRequestCreate
) -> SupportGrantOut:
    operator_id = _require_operator(ctx)
    if data.requested_hours > get_settings().support_max_hours:
        from app.core.errors import ValidationFailed

        raise ValidationFailed(
            "At most 24 hours", details={"fields": {"requested_hours": "At most 24 hours"}}
        )
    async with platform_session() as pdb:
        status = await identity_bridge.tenant_status(pdb, tenant_id)
    if status is None or status == "deleted":
        raise NotFound("Tenant not found")
    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "platform", operator_id)
        row = SupportAccessGrant(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            operator_id=operator_id,
            reason=data.reason.strip(),
            ticket_ref=data.ticket_ref,
            requested_hours=data.requested_hours,
            status="pending",
        )
        db.add(row)
        await db.flush()
        await db.refresh(row)
        db.add(SupportGrantLookup(grant_id=row.id, tenant_id=tenant_id, operator_id=operator_id))
        approvers = await identity_bridge.users_with_permission(db, "support_access:approve")
        out = _out(row)
    await email_users(
        approvers,
        "Vendor support access requested",
        f"A Talent OS support engineer requests read-only access for {data.requested_hours} h.\n\n"
        f"Reason: {data.reason}\nTicket: {data.ticket_ref or '-'}\n\nApprove or deny it in Privacy > Support access.\n",
        tenant_id=tenant_id,
        kind="support_access_requested",
    )
    return out


async def _all_grants(
    operator_id: uuid.UUID | None = None, grant_id: uuid.UUID | None = None
) -> list[SupportAccessGrant]:
    """Operator-side read: the platform lookup says which tenant sessions to open."""
    async with platform_session() as pdb:
        stmt = select(SupportGrantLookup.tenant_id).distinct()
        if operator_id is not None:
            stmt = stmt.where(SupportGrantLookup.operator_id == operator_id)
        if grant_id is not None:
            stmt = stmt.where(SupportGrantLookup.grant_id == grant_id)
        tenant_ids = list((await pdb.execute(stmt)).scalars())
    rows: list[SupportAccessGrant] = []
    for tenant_id in tenant_ids:
        async with tenant_session(tenant_id) as db:
            query = select(SupportAccessGrant)
            if operator_id is not None:
                query = query.where(SupportAccessGrant.operator_id == operator_id)
            if grant_id is not None:
                query = query.where(SupportAccessGrant.id == grant_id)
            rows += list((await db.execute(query)).scalars())
    return sorted(rows, key=lambda r: r.created_at, reverse=True)


async def list_operator_requests(ctx: RequestContext, *, mine_only: bool = False) -> list[SupportGrantOut]:
    operator_id = _require_operator(ctx)
    rows = await _all_grants(operator_id if mine_only else None)
    names = await _operator_names({r.operator_id for r in rows})
    return [_out(r, names.get(r.operator_id)) for r in rows]


async def _find(grant_id: uuid.UUID) -> SupportAccessGrant:
    for row in await _all_grants(grant_id=grant_id):
        return row
    raise NotFound("Support request not found")


async def cancel(ctx: RequestContext, grant_id: uuid.UUID) -> SupportGrantOut:
    operator_id = _require_operator(ctx)
    found = await _find(grant_id)
    if found.operator_id != operator_id:
        raise NotFound("Support request not found")
    async with tenant_session(found.tenant_id) as db:
        audit.set_actor(db, "platform", operator_id)
        row = await db.get(SupportAccessGrant, grant_id, with_for_update=True)
        assert row is not None
        await _transition(db, row, "cancelled", "platform", operator_id)
        return _out(row)


async def start_session(ctx: RequestContext, grant_id: uuid.UUID) -> tuple[SupportSessionOut, str, int]:
    """Open a support session for an approved grant. Returns ``(out, raw cookie token, max_age)``."""
    operator_id = _require_operator(ctx)
    found = await _find(grant_id)
    if found.operator_id != operator_id:
        raise NotFound("Support request not found")
    now = _now()
    if found.status != "approved" or found.expires_at is None or found.expires_at <= now:
        raise ConflictError("The support request is not approved or has expired", code="support_not_approved")
    from app.services import auth_service as identity

    async with platform_session() as pdb:
        issued = await identity.create_session(
            pdb,
            principal_type="support",
            principal_id=operator_id,
            tenant_id=found.tenant_id,
            ip=ctx.ip,
            user_agent=ctx.user_agent,
            absolute_timeout=found.expires_at - now,
        )
        issued.session.support_grant_id = found.id
        await pdb.flush()
    async with tenant_session(found.tenant_id) as db:
        audit.record(
            "support.session_started",
            "support_access_grant",
            found.id,
            session=db,
            actor_type="platform",
            actor_id=operator_id,
        )
    out = SupportSessionOut(
        grant_id=found.id,
        tenant_id=found.tenant_id,
        expires_at=found.expires_at,
        app_url=get_settings().frontend_url.rstrip("/") + "/",
    )
    return out, issued.token, max(1, int((found.expires_at - now).total_seconds()))


# ================================================================ tenant side
async def list_for_tenant(ctx: RequestContext, db: AsyncSession) -> list[SupportGrantOut]:  # ruff: ignore[unused-function-argument]
    rows = list(
        (
            await db.execute(select(SupportAccessGrant).order_by(SupportAccessGrant.created_at.desc()))
        ).scalars()
    )
    names = await _operator_names({r.operator_id for r in rows})
    return [_out(r, names.get(r.operator_id)) for r in rows]


async def decide(ctx: RequestContext, db: AsyncSession, grant_id: uuid.UUID, to: str) -> SupportGrantOut:
    row = await db.get(SupportAccessGrant, grant_id, with_for_update=True)
    if row is None:
        raise NotFound("Support request not found")
    await _transition(db, row, to, "user", getattr(ctx.principal, "id", None))
    return _out(row)


async def expire_grants(db: AsyncSession) -> int:
    """Expire approved grants past ``expires_at`` and stale pending requests (per tenant, every 5 min)."""
    now = _now()
    rows = list(
        (
            await db.execute(
                select(SupportAccessGrant)
                .where(SupportAccessGrant.status.in_(("pending", "approved")))
                .with_for_update()
            )
        ).scalars()
    )
    expired = 0
    for row in rows:
        due = (row.status == "approved" and row.expires_at is not None and row.expires_at <= now) or (
            row.status == "pending" and row.created_at + PENDING_TTL <= now
        )
        if due:
            await _transition(db, row, "expired", "system", None)
            expired += 1
    return expired


# ================================================================ request authentication (support principal)
def _ended() -> Unauthorized:
    return Unauthorized("Support access has ended", code="support_access_ended")


async def resolve_support_principal(request: Request, token: str) -> Principal:
    """Authenticate a ``tos_support`` cookie. No caching: the grant is re-checked on every request."""
    now = _now()
    async with platform_session() as pdb:
        row = (
            await pdb.execute(
                select(AuthSession).where(
                    AuthSession.token_hash == hash_token(token), AuthSession.principal_type == "support"
                )
            )
        ).scalar_one_or_none()
        if row is None or row.tenant_id is None or row.support_grant_id is None:
            raise _ended()
        if row.revoked_at is not None or row.expires_at <= now:
            raise _ended()
        tenant_id, grant_id, operator_id, session_id = (
            row.tenant_id,
            row.support_grant_id,
            row.principal_id,
            row.id,
        )
    async with tenant_session(tenant_id) as db:
        grant = await db.get(SupportAccessGrant, grant_id)
        if grant is None or grant.status != "approved" or grant.expires_at is None or grant.expires_at <= now:
            raise _ended()
    method = request.method.upper()
    path = request.url.path
    if method not in READ_ONLY_METHODS and path not in ALLOWED_WRITE_PATHS:
        raise Forbidden("Support access is read-only", code="support_read_only")
    # Pre-committed audit row (its own transaction): if it cannot be written the request fails.
    route = request.scope.get("route")
    async with tenant_session(tenant_id) as adb:
        audit.record(
            "support.request",
            "support_access_grant",
            grant_id,
            session=adb,
            actor_type="platform",
            actor_id=operator_id,
            method=method,
            route=getattr(route, "path", path),
            grant_id=str(grant_id),
        )
    principal = Principal(
        type="support",
        id=operator_id,
        session_id=session_id,
        tenant_id=tenant_id,
        permissions=support_permissions(),
        read_only=True,
        support_grant_id=grant_id,
    )
    update_context(principal=principal, tenant_id=tenant_id)
    return principal


async def get_support_me(principal: Principal, db: AsyncSession) -> AuthOut:
    """``GET /auth/me`` for a support session: the operator, the tenant, the read-only grants and the grant's
    expiry (``principal.support_grant_id`` was re-checked by ``resolve_support_principal``)."""
    if principal.tenant_id is None or principal.support_grant_id is None:
        raise _ended()
    grant = await db.get(SupportAccessGrant, principal.support_grant_id)
    tenant = await db.get(Tenant, principal.tenant_id)
    if grant is None or grant.expires_at is None or tenant is None:
        raise _ended()
    async with platform_session() as pdb:
        operator = await pdb.get(PlatformOperator, principal.id)
        if operator is None:
            raise _ended()
        operator_id, operator_email, operator_name = operator.id, operator.email, operator.name
    return AuthOut(
        principal_type="support",
        support=SupportSessionInfo(operator_name=operator_name, expires_at=grant.expires_at),
        user=UserOut(
            id=operator_id,
            tenant_id=tenant.id,
            email=operator_email,
            name=operator_name,
            role_keys=[],
            business_unit_id=None,
            mfa_enabled=False,
            last_login_at=None,
        ),
        tenant=TenantSummary.model_validate(tenant),
        permissions={perm: scope.value for perm, scope in sorted(principal.permissions.items())},
        mfa_enrollment_required=False,
    )
