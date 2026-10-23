"""Client User and Vendor User access administration (Epic 14, FR-78, FR-79, AD-26).

Internal (``client_portal:manage`` / ``vendor_portal:manage``):
* Client Users are granted from a Client's Contact (Client 360 "Portal access"): portal permissions, optional
  demand / placement scopes (``restricted``), optional enforced TOTP. Grant emails a sign-in link + code
  (7 days). Suspend / reactivate / revoke; suspension and revocation end every session immediately and emit
  ``ClientUserAccessChanged``.
* Vendor Users are invited by email; deactivate / reactivate; reset TOTP.

Vendor self-administration (Vendor Portal, vendor admins only): list, invite, update, deactivate /
reactivate; the vendor always keeps one active admin (409 ``last_vendor_admin``).

Epic 50 (FR-251): ``jit_client_user`` provisions an active Client User (and its Contact) for a client IdP's
just-in-time sign-in; ``force_client_mfa`` enforces TOTP on a break-glass Client User.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import column, func, select, table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.events.external_auth import ClientUserAccessChanged, VendorUserStatusChanged
from app.models.clients import Contact
from app.models.external_auth import ClientUser, ClientUserScope, VendorUser
from app.models.identity import Tenant
from app.models.vendors import Vendor
from app.schemas.external_auth import (
    ClientUserGrantIn,
    ClientUserOut,
    ClientUserScopeIn,
    ClientUserUpdate,
    VendorUserInviteIn,
    VendorUserOut,
    VendorUserUpdate,
)
from app.schemas.external_auth_portal import (
    VendorPortalUserInviteIn,
    VendorPortalUserOut,
    VendorPortalUserUpdate,
)
from app.services.external_auth import sessions, signin

_SCOPE_TABLES = {
    "demand": table("demands", column("id"), column("client_id")),
    "placement": table("placements", column("id"), column("client_id")),
}


def _principal_id(ctx: RequestContext) -> uuid.UUID | None:
    return getattr(ctx.principal, "id", None)


async def _tenant(db: AsyncSession) -> Tenant:
    raw = db.sync_session.info.get("tenant_id")
    tenant = await db.get(Tenant, uuid.UUID(str(raw))) if raw else None
    if tenant is None:  # pragma: no cover - tenant sessions only
        msg = "external access needs a tenant session"
        raise RuntimeError(msg)
    return tenant


# ================================================================ client users (internal)
async def _scopes(db: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[ClientUserScopeIn]]:
    out: dict[uuid.UUID, list[ClientUserScopeIn]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = (
        await db.execute(select(ClientUserScope).where(ClientUserScope.client_user_id.in_(ids)))
    ).scalars()
    for r in rows:
        out[r.client_user_id].append(ClientUserScopeIn(subject_kind=r.subject_kind, subject_id=r.subject_id))
    return out


def _client_out(cu: ClientUser, scopes: list[ClientUserScopeIn]) -> ClientUserOut:
    return ClientUserOut(
        id=cu.id,
        client_id=cu.client_id,
        contact_id=cu.contact_id,
        email=str(cu.email),
        display_name=cu.display_name,
        status=cu.status,
        portal_permissions=sorted(cu.portal_permissions or ()),
        restricted=cu.restricted,
        scopes=scopes,
        mfa_enforced=cu.mfa_enforced,
        mfa_enabled=cu.mfa_enabled_at is not None,
        last_sign_in_at=cu.last_sign_in_at,
        created_at=cu.created_at,
    )


async def _get_client_user(db: AsyncSession, client_id: uuid.UUID, cu_id: uuid.UUID) -> ClientUser:
    cu = (
        await db.execute(
            select(ClientUser)
            .where(ClientUser.id == cu_id, ClientUser.client_id == client_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cu is None:
        raise NotFound("Client user not found")
    return cu


async def _set_scopes(db: AsyncSession, cu: ClientUser, scopes: list[ClientUserScopeIn]) -> None:
    for s in scopes:
        t = _SCOPE_TABLES[s.subject_kind]
        if not await _table_exists(db, t.name):
            raise ValidationFailed(
                "Unknown scope subject", code="invalid_scope", details={"subject_id": str(s.subject_id)}
            )
        owner = (await db.execute(select(t.c.client_id).where(t.c.id == s.subject_id))).scalar_one_or_none()
        if owner != cu.client_id:
            raise ValidationFailed(
                "The scope must be a demand or placement of this client",
                code="invalid_scope",
                details={"subject_id": str(s.subject_id)},
            )
    existing = (
        await db.execute(select(ClientUserScope).where(ClientUserScope.client_user_id == cu.id))
    ).scalars()
    for row in existing:
        await db.delete(row)
    await db.flush()
    for s in {(s.subject_kind, s.subject_id) for s in scopes}:  # type: ignore[assignment]
        db.add(
            ClientUserScope(
                id=uuid.uuid4(),
                tenant_id=cu.tenant_id,
                client_user_id=cu.id,
                client_id=cu.client_id,
                subject_kind=s[0],  # type: ignore[index]
                subject_id=s[1],  # type: ignore[index]
            )
        )
    await db.flush()


async def _table_exists(db: AsyncSession, name: str) -> bool:
    return bool((await db.execute(select(func.to_regclass(name)))).scalar_one_or_none())


async def _assert_client(db: AsyncSession, client_id: uuid.UUID) -> None:
    from app.models.clients import Client

    if (await db.execute(select(Client.id).where(Client.id == client_id))).first() is None:
        raise NotFound("Client not found")


async def _assert_vendor(db: AsyncSession, vendor_id: uuid.UUID) -> None:
    if (await db.execute(select(Vendor.id).where(Vendor.id == vendor_id))).first() is None:
        raise NotFound("Vendor not found")


async def list_client_users(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
) -> list[ClientUserOut]:
    await _assert_client(db, client_id)
    rows = list(
        (
            await db.execute(
                select(ClientUser).where(ClientUser.client_id == client_id).order_by(ClientUser.display_name)
            )
        ).scalars()
    )
    scopes = await _scopes(db, [r.id for r in rows])
    return [_client_out(r, scopes[r.id]) for r in rows]


async def grant_client_user(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: ClientUserGrantIn
) -> ClientUserOut:
    await _assert_client(db, client_id)
    contact = (
        await db.execute(select(Contact).where(Contact.id == data.contact_id, Contact.client_id == client_id))
    ).scalar_one_or_none()
    if contact is None or contact.archived_at is not None:
        raise ValidationFailed(
            "Choose an active contact of this client", code="invalid_contact", details={"field": "contact_id"}
        )
    if not contact.email:
        raise ValidationFailed(
            "The contact has no email address", code="contact_email_required", details={"field": "contact_id"}
        )
    if data.restricted and not data.scopes:
        raise ValidationFailed(
            "A restricted user needs at least one demand or placement",
            code="scope_required",
            details={"field": "scopes"},
        )
    tenant = await _tenant(db)
    principal_id = _principal_id(ctx)
    cu = ClientUser(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        client_id=client_id,
        contact_id=contact.id,
        email=str(contact.email).lower(),
        display_name=contact.name,
        status="invited",
        portal_permissions=sorted(set(data.portal_permissions)),
        restricted=data.restricted,
        mfa_enforced=data.mfa_enforced,
        invited_by=principal_id if getattr(ctx.principal, "type", None) == "user" else None,
    )
    try:
        async with db.begin_nested():
            db.add(cu)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError("This contact already has portal access", code="client_user_exists") from exc
    await _set_scopes(db, cu, data.scopes)
    audit.record("client_user.access_granted", "client_user", cu.id, session=db, client_id=client_id)
    emit(
        db,
        ClientUserAccessChanged(
            tenant_id=tenant.id, client_user_id=cu.id, client_id=client_id, status=cu.status
        ),
    )
    await signin.issue_client_link(db, tenant, cu, signin.CLIENT_INVITE_TTL, "client_portal_invitation")
    await db.refresh(cu)
    return _client_out(cu, (await _scopes(db, [cu.id]))[cu.id])


async def update_client_user(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    cu_id: uuid.UUID,
    data: ClientUserUpdate,
) -> ClientUserOut:
    cu = await _get_client_user(db, client_id, cu_id)
    if cu.status == "revoked":
        raise ConflictError("Portal access was revoked", code="client_user_revoked")
    sent = data.model_fields_set
    if "portal_permissions" in sent and data.portal_permissions is not None:
        cu.portal_permissions = sorted(set(data.portal_permissions))
    if "restricted" in sent and data.restricted is not None:
        cu.restricted = data.restricted
    if "mfa_enforced" in sent and data.mfa_enforced is not None:
        cu.mfa_enforced = data.mfa_enforced
    if "scopes" in sent and data.scopes is not None:
        await _set_scopes(db, cu, data.scopes)
    if cu.restricted and not (await _scopes(db, [cu.id]))[cu.id]:
        raise ValidationFailed(
            "A restricted user needs at least one demand or placement",
            code="scope_required",
            details={"field": "scopes"},
        )
    await db.flush()
    audit.record("client_user.access_changed", "client_user", cu.id, session=db, fields=sorted(sent))
    await db.refresh(cu)
    return _client_out(cu, (await _scopes(db, [cu.id]))[cu.id])


async def set_client_user_status(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    cu_id: uuid.UUID,
    action: str,
) -> ClientUserOut:
    """``suspend`` | ``reactivate`` | ``revoke`` (final). Suspension / revocation ends every session now."""
    cu = await _get_client_user(db, client_id, cu_id)
    if cu.status == "revoked":
        raise ConflictError("Portal access was revoked", code="client_user_revoked")
    if action == "reactivate":
        target = "active" if cu.last_sign_in_at is not None else "invited"
    else:
        target = {"suspend": "suspended", "revoke": "revoked"}[action]
    if target != cu.status:
        cu.status = target
        if target == "revoked":
            cu.revoked_at = datetime.now(UTC)
        if target in ("suspended", "revoked"):
            await sessions.revoke_all(db, "client_user", cu.id)
            from app.core import secure_links

            await secure_links.revoke_for_subject(db, "client_user", cu.id)
        await db.flush()
        verb = {"suspend": "suspended", "reactivate": "reactivated", "revoke": "revoked"}[action]
        audit.record(f"client_user.{verb}", "client_user", cu.id, session=db)
        emit(
            db,
            ClientUserAccessChanged(
                tenant_id=cu.tenant_id, client_user_id=cu.id, client_id=cu.client_id, status=target
            ),
        )
    await db.refresh(cu)
    return _client_out(cu, (await _scopes(db, [cu.id]))[cu.id])


async def resend_client_invite(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    cu_id: uuid.UUID,
) -> None:
    cu = await _get_client_user(db, client_id, cu_id)
    if cu.status not in ("invited", "active"):
        raise ConflictError("Portal access is not active", code="client_user_inactive")
    await signin.issue_client_link(
        db, await _tenant(db), cu, signin.CLIENT_INVITE_TTL, "client_portal_invitation"
    )
    audit.record("client_user.invitation_resent", "client_user", cu.id, session=db)


async def reset_client_mfa(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    cu_id: uuid.UUID,
) -> ClientUserOut:
    cu = await _get_client_user(db, client_id, cu_id)
    await signin.reset_mfa(db, "client_user", cu)
    await db.refresh(cu)
    return _client_out(cu, (await _scopes(db, [cu.id]))[cu.id])


# ================================================================ Epic 50: client SSO helpers
async def jit_client_user(
    db: AsyncSession, client_id: uuid.UUID, *, email: str, name: str, permissions: Sequence[str]
) -> ClientUser:
    """Create (or reuse an unlinked Contact for) an ``active`` Client User of ``client_id``: portal permissions
    = the IdP's JIT defaults, unrestricted, no enforced TOTP, ``invited_by`` NULL. Emits
    ``ClientUserAccessChanged``. The caller links the identity, audits and notifies."""
    await _assert_client(db, client_id)
    tenant = await _tenant(db)
    email = email.strip().lower()
    contact = (
        await db.execute(
            select(Contact)
            .where(Contact.client_id == client_id, Contact.email == email, Contact.archived_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if contact is not None:
        taken = (await db.execute(select(ClientUser.id).where(ClientUser.contact_id == contact.id))).first()
        if taken is not None:
            raise ConflictError("This contact already has portal access", code="client_user_exists")
    else:
        contact = Contact(
            id=uuid.uuid4(), tenant_id=tenant.id, client_id=client_id, name=name[:200], email=email, type="other"
        )
        db.add(contact)
        await db.flush()
    cu = ClientUser(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        client_id=client_id,
        contact_id=contact.id,
        email=email,
        display_name=(contact.name or name)[:200],
        status="active",
        portal_permissions=sorted(set(permissions)),
        restricted=False,
        mfa_enforced=False,
        invited_by=None,
    )
    db.add(cu)
    await db.flush()
    emit(
        db,
        ClientUserAccessChanged(tenant_id=tenant.id, client_user_id=cu.id, client_id=client_id, status=cu.status),
    )
    return cu


async def force_client_mfa(db: AsyncSession, client_id: uuid.UUID, cu_id: uuid.UUID) -> ClientUser:
    """Break-glass Client Users must use TOTP (``mfa_enforced``); 404 unless the user belongs to the Client."""
    try:
        cu = await _get_client_user(db, client_id, cu_id)
    except NotFound as exc:
        raise NotFound("Client user not found", code="client_user_not_found") from exc
    if not cu.mfa_enforced:
        cu.mfa_enforced = True
        await db.flush()
        audit.record("client_user.access_changed", "client_user", cu.id, session=db, fields=["mfa_enforced"])
    return cu


# ================================================================ vendor users (internal + self-admin)
def _vendor_out(vu: VendorUser) -> VendorUserOut:
    return VendorUserOut(
        id=vu.id,
        vendor_id=vu.vendor_id,
        email=str(vu.email),
        display_name=vu.display_name,
        status=vu.status,
        is_vendor_admin=vu.is_vendor_admin,
        portal_permissions=sorted(vu.portal_permissions or ()),
        mfa_enabled=vu.mfa_enabled_at is not None,
        last_sign_in_at=vu.last_sign_in_at,
        created_at=vu.created_at,
    )


def _vendor_portal_out(vu: VendorUser) -> VendorPortalUserOut:
    return VendorPortalUserOut(
        id=vu.id,
        email=str(vu.email),
        display_name=vu.display_name,
        status=vu.status,
        is_vendor_admin=vu.is_vendor_admin,
        portal_permissions=sorted(vu.portal_permissions or ()),
        mfa_enabled=vu.mfa_enabled_at is not None,
        last_sign_in_at=vu.last_sign_in_at,
    )


async def _get_vendor_user(db: AsyncSession, vendor_id: uuid.UUID, vu_id: uuid.UUID) -> VendorUser:
    vu = (
        await db.execute(
            select(VendorUser)
            .where(VendorUser.id == vu_id, VendorUser.vendor_id == vendor_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if vu is None:
        raise NotFound("Vendor user not found")
    return vu


async def _invite_vendor_user(
    db: AsyncSession,
    vendor_id: uuid.UUID,
    *,
    email: str,
    display_name: str,
    is_admin: bool,
    permissions: list[str],
    invited_by_kind: str,
    invited_by_id: uuid.UUID | None,
) -> VendorUser:
    vendor = (await db.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one_or_none()
    if vendor is None:
        raise NotFound("Vendor not found")
    if vendor.status not in signin.VENDOR_OK_STATUSES:
        raise ConflictError("The vendor is not active", code="vendor_inactive")
    tenant = await _tenant(db)
    vu = VendorUser(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        vendor_id=vendor_id,
        email=email.lower(),
        display_name=display_name,
        status="invited",
        is_vendor_admin=is_admin,
        portal_permissions=sorted(set(permissions)),
        invited_by_kind=invited_by_kind,
        invited_by_id=invited_by_id,
    )
    try:
        async with db.begin_nested():
            db.add(vu)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "A vendor user with this email already exists", code="vendor_user_exists"
        ) from exc
    audit.record("vendor_user.invited", "vendor_user", vu.id, session=db, vendor_id=vendor_id)
    emit(
        db,
        VendorUserStatusChanged(
            tenant_id=tenant.id, vendor_user_id=vu.id, vendor_id=vendor_id, status="invited"
        ),
    )
    await signin.send_vendor_invitation(db, tenant, vu)
    await db.refresh(vu)
    return vu


async def _active_admins(db: AsyncSession, vendor_id: uuid.UUID, exclude: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).where(
                    VendorUser.vendor_id == vendor_id,
                    VendorUser.is_vendor_admin.is_(True),
                    VendorUser.status == "active",
                    VendorUser.id != exclude,
                )
            )
        ).scalar_one()
    )


async def _set_vendor_status(
    db: AsyncSession, vu: VendorUser, active: bool, *, guard_last_admin: bool
) -> None:
    if not active and vu.status != "deactivated":
        if (
            guard_last_admin
            and vu.is_vendor_admin
            and vu.status == "active"
            and not await _active_admins(db, vu.vendor_id, vu.id)
        ):
            raise signin.conflict_last_admin()
        vu.status = "deactivated"
        await sessions.revoke_all(db, "vendor_user", vu.id)
        from app.core import secure_links

        await secure_links.revoke_for_subject(db, "vendor_user", vu.id)
    elif active and vu.status == "deactivated":
        vu.status = "active" if vu.mfa_enabled_at is not None and vu.password_hash else "invited"
    else:
        return
    await db.flush()
    audit.record(
        f"vendor_user.{'reactivated' if active else 'deactivated'}", "vendor_user", vu.id, session=db
    )
    emit(
        db,
        VendorUserStatusChanged(
            tenant_id=vu.tenant_id, vendor_user_id=vu.id, vendor_id=vu.vendor_id, status=vu.status
        ),
    )


async def list_vendor_users(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    vendor_id: uuid.UUID,
) -> list[VendorUserOut]:
    await _assert_vendor(db, vendor_id)
    rows = (
        await db.execute(
            select(VendorUser).where(VendorUser.vendor_id == vendor_id).order_by(VendorUser.display_name)
        )
    ).scalars()
    return [_vendor_out(r) for r in rows]


async def invite_vendor_user(
    ctx: RequestContext, db: AsyncSession, vendor_id: uuid.UUID, data: VendorUserInviteIn
) -> VendorUserOut:
    vu = await _invite_vendor_user(
        db,
        vendor_id,
        email=str(data.email),
        display_name=data.display_name,
        is_admin=data.is_vendor_admin,
        permissions=list(data.portal_permissions),
        invited_by_kind="user",
        invited_by_id=_principal_id(ctx),
    )
    return _vendor_out(vu)


async def update_vendor_user(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    vendor_id: uuid.UUID,
    vu_id: uuid.UUID,
    data: VendorUserUpdate,
) -> VendorUserOut:
    vu = await _get_vendor_user(db, vendor_id, vu_id)
    await _apply_vendor_update(
        db, vu, data.display_name, data.is_vendor_admin, data.portal_permissions, guard=False
    )
    return _vendor_out(vu)


async def set_vendor_user_status(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    vendor_id: uuid.UUID,
    vu_id: uuid.UUID,
    *,
    active: bool,
) -> VendorUserOut:
    vu = await _get_vendor_user(db, vendor_id, vu_id)
    await _set_vendor_status(db, vu, active, guard_last_admin=False)
    await db.refresh(vu)
    return _vendor_out(vu)


async def resend_vendor_invite(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    vendor_id: uuid.UUID,
    vu_id: uuid.UUID,
) -> None:
    vu = await _get_vendor_user(db, vendor_id, vu_id)
    if vu.status != "invited":
        raise ConflictError("The vendor user already accepted the invitation", code="vendor_user_not_invited")
    await signin.send_vendor_invitation(db, await _tenant(db), vu)


async def reset_vendor_mfa(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    vendor_id: uuid.UUID,
    vu_id: uuid.UUID,
) -> VendorUserOut:
    vu = await _get_vendor_user(db, vendor_id, vu_id)
    await signin.reset_mfa(db, "vendor_user", vu)
    await db.refresh(vu)
    return _vendor_out(vu)


async def _apply_vendor_update(
    db: AsyncSession,
    vu: VendorUser,
    display_name: str | None,
    is_admin: bool | None,
    permissions: list[str] | None,
    *,
    guard: bool,
) -> None:
    if display_name:
        vu.display_name = display_name
    if is_admin is not None and is_admin != vu.is_vendor_admin:
        if (
            not is_admin
            and guard
            and vu.status == "active"
            and not await _active_admins(db, vu.vendor_id, vu.id)
        ):
            raise signin.conflict_last_admin()
        vu.is_vendor_admin = is_admin
    if permissions is not None:
        vu.portal_permissions = sorted(set(permissions))
    await db.flush()
    audit.record("vendor_user.updated", "vendor_user", vu.id, session=db)
    await db.refresh(vu)


# ---------------------------------------------------------------- vendor self-administration (portal)
def _require_vendor_admin(ctx: RequestContext) -> tuple[uuid.UUID, uuid.UUID]:
    principal = ctx.principal
    if principal is None or principal.type != "vendor_user" or principal.org_id is None:
        raise Forbidden(
            "Only vendor admins can manage users", code="vendor_admin_required", ask="account_manager"
        )
    return principal.id, principal.org_id


async def _assert_admin(db: AsyncSession, ctx: RequestContext) -> tuple[uuid.UUID, uuid.UUID]:
    me_id, vendor_id = _require_vendor_admin(ctx)
    is_admin = (
        await db.execute(select(VendorUser.is_vendor_admin).where(VendorUser.id == me_id))
    ).scalar_one_or_none()
    if not is_admin:
        raise Forbidden(
            "Only vendor admins can manage users",
            code="vendor_admin_required",
            action="Manage vendor portal users",
            ask="account_manager",
        )
    return me_id, vendor_id


async def portal_list_users(ctx: RequestContext, db: AsyncSession) -> list[VendorPortalUserOut]:
    _, vendor_id = await _assert_admin(db, ctx)
    rows = (
        await db.execute(
            select(VendorUser).where(VendorUser.vendor_id == vendor_id).order_by(VendorUser.display_name)
        )
    ).scalars()
    return [_vendor_portal_out(r) for r in rows]


async def portal_invite_user(
    ctx: RequestContext, db: AsyncSession, data: VendorPortalUserInviteIn
) -> VendorPortalUserOut:
    me_id, vendor_id = await _assert_admin(db, ctx)
    vu = await _invite_vendor_user(
        db,
        vendor_id,
        email=str(data.email),
        display_name=data.display_name,
        is_admin=data.is_vendor_admin,
        permissions=list(data.portal_permissions),
        invited_by_kind="vendor_user",
        invited_by_id=me_id,
    )
    return _vendor_portal_out(vu)


async def portal_update_user(
    ctx: RequestContext, db: AsyncSession, vu_id: uuid.UUID, data: VendorPortalUserUpdate
) -> VendorPortalUserOut:
    _, vendor_id = await _assert_admin(db, ctx)
    vu = await _get_vendor_user(db, vendor_id, vu_id)
    await _apply_vendor_update(
        db, vu, data.display_name, data.is_vendor_admin, data.portal_permissions, guard=True
    )
    return _vendor_portal_out(vu)


async def portal_set_status(
    ctx: RequestContext, db: AsyncSession, vu_id: uuid.UUID, *, active: bool
) -> VendorPortalUserOut:
    _, vendor_id = await _assert_admin(db, ctx)
    vu = await _get_vendor_user(db, vendor_id, vu_id)
    await _set_vendor_status(db, vu, active, guard_last_admin=True)
    await db.refresh(vu)
    return _vendor_portal_out(vu)
