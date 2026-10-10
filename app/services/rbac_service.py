"""Identity administration (story 1.5): users, invitations, roles, teams and business units of a tenant,
plus the platform-operator resend/revoke of a tenant's first-admin invitation.

Every tenant function takes the request's RLS-scoped session (``TenantDBSessionDep``), so ids of another
tenant are simply invisible (404). Ids referenced in request bodies are checked the same way (422
``invalid_reference``), and the composite foreign keys make cross-tenant links impossible in the DB too.

Invariants enforced here:

* the tenant keeps at least one active user holding the Tenant Admin role (409 ``last_admin``); changes
  that could break it lock the Tenant Admin role row ``FOR UPDATE`` so concurrent changes serialize;
* seats: active users + pending invitations must stay below ``tenants.seat_limit`` to invite or
  reactivate (409 ``seat_limit_reached``); the tenant row is locked ``FOR UPDATE`` meanwhile;
* system roles cannot be deleted or re-keyed; Tenant Admin's permissions are immutable (409 ``system_role``);
* deactivation revokes every session of the user immediately;
* no privilege escalation: roles can only be built, assigned or removed by a caller holding every
  permission involved at least at the granted scope (403 ``forbidden``, ``details.permission``).
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime

from fastapi import BackgroundTasks
from sqlalchemy import Select, column, func, or_, select, table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.mailer import EmailMessage
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import (
    CATALOG,
    SENSITIVE_PERMISSIONS,
    TENANT_ADMIN,
    InvalidGrant,
    Principal,
    Scope,
    check,
    effective_role_grants,
    forbidden,
    validate_grants,
)
from app.core.settings import get_settings
from app.models.identity import BusinessUnit, Invitation, Role, Team, TeamMember, Tenant, User, UserRole
from app.schemas.identity import (
    AdminUserOut,
    BusinessUnitCreate,
    BusinessUnitOut,
    BusinessUnitRef,
    BusinessUnitUpdate,
    InvitationAdminOut,
    PermissionOut,
    RoleCreate,
    RoleOut,
    RoleRef,
    RoleUpdate,
    TeamCreate,
    TeamDetailOut,
    TeamMemberOut,
    TeamMemberSet,
    TeamOut,
    TeamRef,
    TeamUpdate,
    UserFilters,
    UserInvite,
    UserUpdate,
)
from app.services import access_service as access
from app.services import auth_service as service

ROLE_KEY_CONSTRAINT = "uq_roles_tenant_id_key"
TEAM_NAME_CONSTRAINT = "uq_teams_tenant_id_name"
BU_NAME_CONSTRAINT = "uq_business_units_tenant_id_name"


def _now() -> datetime:
    return datetime.now(UTC)


def _conflict_on(exc: IntegrityError, constraint: str, error: ConflictError) -> ConflictError:
    if service.violated_constraint(exc) != constraint:
        raise exc
    return error


def invalid_reference(field: str, ids: Iterable[uuid.UUID]) -> ValidationFailed:
    missing = sorted(str(i) for i in ids)
    message = f"Unknown {field.replace('_', ' ')}: {', '.join(missing)}"
    return ValidationFailed(
        message,
        code="invalid_reference",
        details={"fields": [{"field": field, "message": message, "type": "invalid_reference"}]},
    )


async def _check_references(
    db: AsyncSession,
    *,
    role_ids: Sequence[uuid.UUID] | None = None,
    business_unit_id: uuid.UUID | None = None,
    team_ids: Sequence[uuid.UUID] | None = None,
) -> None:
    """422 ``invalid_reference`` for ids that do not exist in this tenant (other tenants' ids included)."""
    if role_ids:
        missing = set(role_ids) - await access.existing_role_ids(db, role_ids)
        if missing:
            raise invalid_reference("role_ids", missing)
    if business_unit_id is not None and not await access.business_unit_exists(db, business_unit_id):
        raise invalid_reference("business_unit_id", [business_unit_id])
    if team_ids:
        missing = set(team_ids) - await access.existing_team_ids(db, team_ids)
        if missing:
            raise invalid_reference("team_ids", missing)


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ================================================================ seats and admins
async def _lock_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await access.lock_tenant(db, tenant_id)
    if tenant is None:  # pragma: no cover - the principal's tenant always exists
        raise NotFound("Tenant not found")
    return tenant


seats_used = access.seats_used


def _seat_limit_reached() -> ConflictError:
    return ConflictError("This workspace has no free seats", code="seat_limit_reached")


async def _ensure_other_admin(db: AsyncSession, admin_role_id: uuid.UUID, losing_user_id: uuid.UUID) -> None:
    """409 ``last_admin`` unless an active user other than ``losing_user_id`` holds Tenant Admin.

    Callers hold the Tenant Admin role row ``FOR UPDATE`` (``access.tenant_admin_role_id``).
    """
    others = (
        await db.execute(
            select(func.count(func.distinct(User.id)))
            .select_from(User)
            .join(UserRole, UserRole.user_id == User.id)
            .where(UserRole.role_id == admin_role_id, User.is_active.is_(True), User.id != losing_user_id)
        )
    ).scalar_one()
    if not others:
        raise ConflictError("The workspace must keep at least one active Tenant Admin", code="last_admin")


async def _ensure_break_glass(db: AsyncSession, losing_user_id: uuid.UUID) -> None:
    """Epic 14 (AD-30): 409 ``break_glass_required`` when this removes the last break-glass admin while SSO is
    enforced."""
    await service.run_admin_loss_guards(db, losing_user_id)


def ensure_can_grant(principal: Principal, grants: Mapping[str, str]) -> None:
    """No privilege escalation: 403 ``forbidden`` unless ``principal`` holds every grant at >= its scope.

    Applies to building roles and to assigning / removing roles, so a holder of ``user:manage`` or
    ``role:manage`` cannot hand out (or take away) more than they have themselves.
    """
    for permission, raw in sorted(grants.items()):
        try:
            scope = Scope(raw)
        except ValueError:
            continue  # rejected by validate_grants
        if permission in CATALOG and not principal.has(permission, scope):
            raise forbidden(permission)


async def _ensure_can_grant_roles(
    db: AsyncSession, principal: Principal, role_ids: Iterable[uuid.UUID]
) -> None:
    ids = set(role_ids)
    if not ids:
        return
    rows = await db.execute(select(Role.key, Role.is_system, Role.permissions).where(Role.id.in_(ids)))
    for key, is_system, grants in rows.tuples():
        ensure_can_grant(principal, effective_role_grants(key, is_system, grants))


async def _user_role_ids(db: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    return set((await db.execute(select(UserRole.role_id).where(UserRole.user_id == user_id))).scalars())


def _self_scope_change() -> ConflictError:
    return ConflictError("You cannot change your own teams or business unit", code="self_scope_change")


async def _holds_role(db: AsyncSession, user_id: uuid.UUID, role_id: uuid.UUID) -> bool:
    row = await db.execute(
        select(UserRole.user_id).where(UserRole.user_id == user_id, UserRole.role_id == role_id)
    )
    return row.first() is not None


# ================================================================ users
async def _admin_users_out(db: AsyncSession, users: Sequence[User]) -> list[AdminUserOut]:
    ids = [u.id for u in users]
    roles: dict[uuid.UUID, list[RoleRef]] = defaultdict(list)
    teams: dict[uuid.UUID, list[TeamRef]] = defaultdict(list)
    bus: dict[uuid.UUID, BusinessUnitRef] = {}
    if ids:
        role_rows = await db.execute(
            select(UserRole.user_id, Role.id, Role.key, Role.name)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id.in_(ids))
            .order_by(Role.name)
        )
        for user_id, role_id, key, name in role_rows.tuples():
            roles[user_id].append(RoleRef(id=role_id, key=key, name=name))
        team_rows = await db.execute(
            select(TeamMember.user_id, Team.id, Team.name, TeamMember.is_lead)
            .join(Team, Team.id == TeamMember.team_id)
            .where(TeamMember.user_id.in_(ids))
            .order_by(Team.name)
        )
        for user_id, team_id, name, is_lead in team_rows.tuples():
            teams[user_id].append(TeamRef(id=team_id, name=name, is_lead=is_lead))
        bu_ids = {u.business_unit_id for u in users if u.business_unit_id is not None}
        if bu_ids:
            bu_rows = await db.execute(
                select(BusinessUnit.id, BusinessUnit.name).where(BusinessUnit.id.in_(bu_ids))
            )
            bus = {bu_id: BusinessUnitRef(id=bu_id, name=name) for bu_id, name in bu_rows.tuples()}
    return [
        AdminUserOut(
            id=u.id,
            email=u.email,
            name=u.name,
            is_active=u.is_active,
            role_keys=sorted(r.key for r in roles[u.id]),
            roles=roles[u.id],
            business_unit=bus.get(u.business_unit_id) if u.business_unit_id else None,
            teams=teams[u.id],
            mfa_enabled=u.mfa_enabled,
            last_login_at=u.last_login_at,
            created_at=u.created_at,
        )
        for u in users
    ]


async def _admin_user_out(db: AsyncSession, user: User) -> AdminUserOut:
    (out,) = await _admin_users_out(db, [user])
    return out


async def _get_user(db: AsyncSession, user_id: uuid.UUID, *, for_update: bool = False) -> User:
    user = await db.get(User, user_id, with_for_update=for_update)
    if user is None:
        raise NotFound("User not found")
    return user


async def list_users(
    ctx: RequestContext, db: AsyncSession, filters: UserFilters, params: PageParams
) -> Page[AdminUserOut]:
    service.require_user(ctx)
    stmt: Select[tuple[User]] = select(User)
    if filters.q:
        pattern = _like(filters.q.strip())
        stmt = stmt.where(or_(User.name.ilike(pattern, escape="\\"), User.email.ilike(pattern, escape="\\")))
    if filters.is_active is not None:
        stmt = stmt.where(User.is_active.is_(filters.is_active))
    if filters.role_id is not None:
        stmt = stmt.where(User.id.in_(select(UserRole.user_id).where(UserRole.role_id == filters.role_id)))
    if filters.team_id is not None:
        stmt = stmt.where(
            User.id.in_(select(TeamMember.user_id).where(TeamMember.team_id == filters.team_id))
        )
    if filters.business_unit_id is not None:
        stmt = stmt.where(User.business_unit_id == filters.business_unit_id)
    page = await paginate(db, stmt.order_by(User.name, User.id), params)
    return Page[AdminUserOut](
        items=await _admin_users_out(db, page.items),
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def get_user(ctx: RequestContext, db: AsyncSession, user_id: uuid.UUID) -> AdminUserOut:
    service.require_user(ctx)
    return await _admin_user_out(db, await _get_user(db, user_id))


async def update_user(
    ctx: RequestContext, db: AsyncSession, user_id: uuid.UUID, data: UserUpdate
) -> AdminUserOut:
    """Partial update of name / roles (replace) / business unit / teams (replace)."""
    principal = service.require_user(ctx)
    user = await _get_user(db, user_id, for_update=True)
    sent = data.model_fields_set
    if "team_ids" in sent:
        check(principal, "team:manage")
    if "business_unit_id" in sent:
        check(principal, "business_unit:manage")
    if user.id == principal.id and sent & {"team_ids", "business_unit_id"}:
        raise _self_scope_change()
    await _check_references(
        db,
        role_ids=data.role_ids if "role_ids" in sent else None,
        business_unit_id=data.business_unit_id if "business_unit_id" in sent else None,
        team_ids=data.team_ids if "team_ids" in sent else None,
    )
    if "name" in sent and data.name is not None and data.name != user.name:
        user.name = data.name
    if "business_unit_id" in sent and data.business_unit_id != user.business_unit_id:
        user.business_unit_id = data.business_unit_id
    if "role_ids" in sent and data.role_ids is not None:
        current = await _user_role_ids(db, user.id)
        await _ensure_can_grant_roles(db, principal, current.symmetric_difference(data.role_ids))
        admin_role_id = await access.tenant_admin_role_id(db, for_update=True)
        if (
            admin_role_id is not None
            and user.is_active
            and admin_role_id not in data.role_ids
            and await _holds_role(db, user.id, admin_role_id)
        ):
            await _ensure_other_admin(db, admin_role_id, user.id)
            await _ensure_break_glass(db, user.id)
        await access.set_user_roles(db, user.tenant_id, user.id, data.role_ids)
    if "team_ids" in sent and data.team_ids is not None:
        await access.set_user_teams(db, user.tenant_id, user.id, data.team_ids)
    await db.flush()  # audited by the listener (user.updated / user_role.* / team_member.*)
    await db.refresh(user)
    return await _admin_user_out(db, user)


async def deactivate_user(ctx: RequestContext, db: AsyncSession, user_id: uuid.UUID) -> AdminUserOut:
    """Deactivate and revoke every session of the user (idempotent for an inactive user)."""
    principal = service.require_user(ctx)
    if user_id == principal.id:
        raise ConflictError("You cannot deactivate your own account", code="cannot_deactivate_self")
    user = await _get_user(db, user_id, for_update=True)
    await _ensure_can_grant_roles(db, principal, await _user_role_ids(db, user.id))
    revoked = 0
    if user.is_active:
        admin_role_id = await access.tenant_admin_role_id(db, for_update=True)
        if admin_role_id is not None and await _holds_role(db, user.id, admin_role_id):
            await _ensure_other_admin(db, admin_role_id, user.id)
            await _ensure_break_glass(db, user.id)
        user.is_active = False
        revoked = await service.revoke_principal_sessions(db, "user", user.id)
        await db.flush()
        await db.refresh(user)
        audit.record("user.deactivated", "user", user.id, session=db, revoked_sessions=revoked)
    return await _admin_user_out(db, user)


async def reactivate_user(ctx: RequestContext, db: AsyncSession, user_id: uuid.UUID) -> AdminUserOut:
    principal = service.require_user(ctx)
    assert principal.tenant_id is not None
    user = await _get_user(db, user_id, for_update=True)
    await _ensure_can_grant_roles(db, principal, await _user_role_ids(db, user.id))
    if not user.is_active:
        tenant = await _lock_tenant(db, principal.tenant_id)
        if await seats_used(db) >= tenant.seat_limit:
            raise _seat_limit_reached()
        user.is_active = True
        service.reset_login_throttle(user)
        await db.flush()
        await db.refresh(user)
        audit.record("user.reactivated", "user", user.id, session=db)
    return await _admin_user_out(db, user)


# ================================================================ invitations
def _invitation_out(invitation: Invitation) -> InvitationAdminOut:
    return InvitationAdminOut(
        id=invitation.id,
        email=invitation.email,
        name=invitation.name,
        role_ids=list(invitation.role_ids),
        business_unit_id=invitation.business_unit_id,
        team_ids=list(invitation.team_ids),
        invited_by_type=invitation.invited_by_type,
        expires_at=invitation.expires_at,
        expired=invitation.expires_at <= _now(),
        created_at=invitation.created_at,
    )


def user_invitation_email(tenant: Tenant, email: str, token: str) -> EmailMessage:
    link = service.accept_invite_url(token)
    hours = get_settings().invitation_ttl_hours
    text = (
        f"Hello,\n\n"
        f"You have been invited to join {tenant.name} on Talent OS.\n\n"
        f"Accept the invitation and create your account here:\n{link}\n\n"
        f"This link can be used once and expires in {hours} hours.\n"
        f"If you were not expecting this invitation, you can ignore this email.\n"
    )
    return EmailMessage(
        to=email,
        subject=f"You're invited to join {tenant.name} on Talent OS",
        text=text,
        tenant_id=tenant.id,
        kind="user_invitation",
    )


def _invitation_email(tenant: Tenant, invitation: Invitation, token: str) -> EmailMessage:
    if invitation.invited_by_type == "platform":
        return service.tenant_admin_invitation_email(tenant, invitation.email, token)
    return user_invitation_email(tenant, invitation.email, token)


async def invite_user(
    ctx: RequestContext, db: AsyncSession, data: UserInvite, background: BackgroundTasks
) -> InvitationAdminOut:
    """Invite a user with roles (+ optional business unit / teams); the email goes out after the response."""
    principal = service.require_user(ctx)
    assert principal.tenant_id is not None
    tenant = await _lock_tenant(db, principal.tenant_id)
    await _check_references(
        db, role_ids=data.role_ids, business_unit_id=data.business_unit_id, team_ids=data.team_ids
    )
    await _ensure_can_grant_roles(db, principal, data.role_ids)
    if (await db.execute(select(User.id).where(User.email == data.email))).first():
        raise ConflictError("A user with this email already exists", code="user_exists")
    pending = await db.execute(
        select(Invitation.id).where(
            Invitation.email == data.email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > _now(),
        )
    )
    if pending.first():
        raise ConflictError(
            "A pending invitation already exists for this email; resend it instead", code="invitation_pending"
        )
    if await seats_used(db) >= tenant.seat_limit:
        raise _seat_limit_reached()
    issued = await service.create_invitation(
        db,
        tenant_id=tenant.id,
        email=data.email,
        name=data.name,
        role_ids=data.role_ids,
        business_unit_id=data.business_unit_id,
        team_ids=data.team_ids,
        invited_by_type="user",
        invited_by_id=principal.id,
    )
    await db.refresh(issued.invitation)
    background.add_task(service.deliver_email, user_invitation_email(tenant, data.email, issued.token))
    return _invitation_out(issued.invitation)


async def list_invitations(
    ctx: RequestContext, db: AsyncSession, params: PageParams
) -> Page[InvitationAdminOut]:
    """Pending invitations (not accepted, not revoked), newest first; expired ones are flagged."""
    service.require_user(ctx)
    stmt = (
        select(Invitation)
        .where(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))
        .order_by(Invitation.created_at.desc(), Invitation.id)
    )
    page = await paginate(db, stmt, params)
    return Page[InvitationAdminOut](
        items=[_invitation_out(i) for i in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def _pending_invitation(db: AsyncSession, invitation_id: uuid.UUID) -> Invitation:
    invitation = await db.get(Invitation, invitation_id, with_for_update=True)
    if invitation is None:
        raise NotFound("Invitation not found")
    if invitation.accepted_at is not None:
        raise ConflictError("This invitation was already accepted", code="invitation_accepted")
    return invitation


async def resend_invitation(
    ctx: RequestContext, db: AsyncSession, invitation_id: uuid.UUID, background: BackgroundTasks
) -> InvitationAdminOut:
    """Email a new token with a new expiry; the previous link stops working."""
    principal = service.require_user(ctx)
    assert principal.tenant_id is not None
    tenant = await _lock_tenant(db, principal.tenant_id)
    invitation = await _pending_invitation(db, invitation_id)
    if invitation.revoked_at is not None:
        raise ConflictError("This invitation was revoked", code="invitation_revoked")
    await _ensure_can_grant_roles(db, principal, invitation.role_ids)
    # An expired invitation no longer holds a seat: re-issuing it consumes one again.
    if invitation.expires_at <= _now() and await seats_used(db) >= tenant.seat_limit:
        raise _seat_limit_reached()
    token = service.rotate_invitation_token(invitation)
    await db.flush()
    await db.refresh(invitation)
    background.add_task(service.deliver_email, _invitation_email(tenant, invitation, token))
    audit.record("invitation.resent", "invitation", invitation.id, session=db)
    return _invitation_out(invitation)


async def revoke_invitation(ctx: RequestContext, db: AsyncSession, invitation_id: uuid.UUID) -> None:
    principal = service.require_user(ctx)
    invitation = await _pending_invitation(db, invitation_id)
    await _ensure_can_grant_roles(db, principal, invitation.role_ids)
    if invitation.revoked_at is None:
        invitation.revoked_at = _now()
        await db.flush()
        audit.record("invitation.revoked", "invitation", invitation.id, session=db)


# ---------------------------------------------------------------- operator: tenant's first admin
async def _operator_tenant(tenant_id: uuid.UUID) -> Tenant:
    async with platform_session() as pdb:
        tenant = await pdb.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Tenant not found")
    if tenant.status != "active":
        raise ConflictError("The tenant is suspended", code="tenant_suspended")
    return tenant


async def resend_tenant_admin_invitation(
    ctx: RequestContext, tenant_id: uuid.UUID, background: BackgroundTasks
) -> InvitationAdminOut:
    """Operator: re-issue the tenant's latest platform (first-admin) invitation with a new token/expiry.

    Works for pending, expired and revoked invitations; 409 ``invitation_accepted`` once accepted,
    404 when the tenant has no platform invitation.
    """
    service.require_platform(ctx)
    tenant = await _operator_tenant(tenant_id)
    async with tenant_session(tenant.id) as db:
        invitation = (
            await db.execute(
                select(Invitation)
                .where(Invitation.invited_by_type == "platform")
                .order_by(Invitation.created_at.desc(), Invitation.id)
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if invitation is None:
            raise NotFound("The tenant has no admin invitation")
        if invitation.accepted_at is not None:
            raise ConflictError("The admin invitation was already accepted", code="invitation_accepted")
        role_ids = await access.seed_default_roles(db, tenant.id)  # idempotent safety net
        if not invitation.role_ids:
            invitation.role_ids = [role_ids[TENANT_ADMIN]]
        token = service.rotate_invitation_token(invitation)
        await db.flush()
        await db.refresh(invitation)
        out = _invitation_out(invitation)
        email = invitation.email
        audit.record("invitation.resent", "invitation", out.id, session=db)
    background.add_task(service.deliver_email, service.tenant_admin_invitation_email(tenant, email, token))
    return out


async def revoke_tenant_admin_invitation(ctx: RequestContext, tenant_id: uuid.UUID) -> None:
    """Operator: revoke the tenant's pending platform invitations (409 ``no_pending_invitation``)."""
    service.require_platform(ctx)
    tenant = await _operator_tenant(tenant_id)
    async with tenant_session(tenant.id) as db:
        pending = (
            (
                await db.execute(
                    select(Invitation)
                    .where(
                        Invitation.invited_by_type == "platform",
                        Invitation.accepted_at.is_(None),
                        Invitation.revoked_at.is_(None),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if not pending:
            raise ConflictError("The tenant has no pending admin invitation", code="no_pending_invitation")
        now = _now()
        for invitation in pending:
            invitation.revoked_at = now
            audit.record("invitation.revoked", "invitation", invitation.id, session=db)


# ================================================================ roles
def permission_catalog() -> list[PermissionOut]:
    return [
        PermissionOut(
            key=p.key,
            group=p.group,
            description=p.description,
            scopes=[s.value for s in p.scopes],
            sensitive=p.key in SENSITIVE_PERMISSIONS,
        )
        for p in CATALOG.values()
    ]


def _is_immutable(role: Role) -> bool:
    return role.is_system and role.key == TENANT_ADMIN


def _role_out(role: Role, user_count: int) -> RoleOut:
    return RoleOut(
        id=role.id,
        key=role.key,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        is_immutable=_is_immutable(role),
        permissions=dict(sorted(role.permissions.items())),
        user_count=user_count,
        created_at=role.created_at,
        updated_at=role.updated_at,
    )


async def _role_user_count(db: AsyncSession, role_id: uuid.UUID) -> int:
    count = (
        await db.execute(select(func.count()).select_from(UserRole).where(UserRole.role_id == role_id))
    ).scalar_one()
    return int(count)


def _grants(permissions: dict[str, str]) -> dict[str, str]:
    try:
        return {k: v.value for k, v in validate_grants(permissions).items()}
    except InvalidGrant as exc:
        message = f"Invalid permission grant {exc.permission}: {exc.reason}"
        raise ValidationFailed(
            message,
            code="invalid_permission",
            details={
                "permission": exc.permission,
                "fields": [
                    {
                        "field": f"permissions.{exc.permission}",
                        "message": exc.reason,
                        "type": "invalid_permission",
                    }
                ],
            },
        ) from exc


def _role_exists() -> ConflictError:
    return ConflictError("A role with this key already exists", code="role_exists")


def _system_role(message: str) -> ConflictError:
    return ConflictError(message, code="system_role")


async def list_roles(ctx: RequestContext, db: AsyncSession) -> list[RoleOut]:
    service.require_user(ctx)
    counts = select(UserRole.role_id, func.count().label("n")).group_by(UserRole.role_id).subquery()
    rows = await db.execute(
        select(Role, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.role_id == Role.id)
        .order_by(Role.is_system.desc(), Role.name, Role.id)
    )
    return [_role_out(role, int(n)) for role, n in rows.tuples()]


async def _get_role(db: AsyncSession, role_id: uuid.UUID, *, for_update: bool = False) -> Role:
    role = await db.get(Role, role_id, with_for_update=for_update)
    if role is None:
        raise NotFound("Role not found")
    return role


async def get_role(ctx: RequestContext, db: AsyncSession, role_id: uuid.UUID) -> RoleOut:
    service.require_user(ctx)
    role = await _get_role(db, role_id)
    return _role_out(role, await _role_user_count(db, role.id))


async def create_role(ctx: RequestContext, db: AsyncSession, data: RoleCreate) -> RoleOut:
    principal = service.require_user(ctx)
    assert principal.tenant_id is not None
    grants = _grants(data.permissions)
    ensure_can_grant(principal, grants)
    if (await db.execute(select(Role.id).where(Role.key == data.key))).first():
        raise _role_exists()
    role = Role(
        tenant_id=principal.tenant_id,
        key=data.key,
        name=data.name,
        description=data.description,
        is_system=False,
        permissions=grants,
    )
    db.add(role)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _conflict_on(exc, ROLE_KEY_CONSTRAINT, _role_exists()) from exc
    await db.refresh(role)
    return _role_out(role, 0)


async def update_role(ctx: RequestContext, db: AsyncSession, role_id: uuid.UUID, data: RoleUpdate) -> RoleOut:
    principal = service.require_user(ctx)
    role = await _get_role(db, role_id, for_update=True)
    sent = data.model_fields_set
    if "key" in sent and data.key is not None and data.key != role.key:
        if role.is_system:
            raise _system_role("The key of a system role cannot be changed")
        if (await db.execute(select(Role.id).where(Role.key == data.key))).first():
            raise _role_exists()
        role.key = data.key
    if "permissions" in sent and data.permissions is not None:
        grants = _grants(data.permissions)
        if _is_immutable(role):
            if grants != role.permissions:
                raise _system_role("The Tenant Admin role's permissions cannot be changed")
        else:
            # Old and new grants separately: no widening beyond, or narrowing of, what the caller holds.
            ensure_can_grant(principal, role.permissions)
            ensure_can_grant(principal, grants)
            role.permissions = grants
    if "name" in sent and data.name is not None:
        role.name = data.name
    if "description" in sent:
        role.description = data.description
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _conflict_on(exc, ROLE_KEY_CONSTRAINT, _role_exists()) from exc
    await db.refresh(role)
    return _role_out(role, await _role_user_count(db, role.id))


async def delete_role(ctx: RequestContext, db: AsyncSession, role_id: uuid.UUID) -> None:
    service.require_user(ctx)
    role = await _get_role(db, role_id, for_update=True)
    if role.is_system:
        raise _system_role("System roles cannot be deleted")
    in_use = await _role_user_count(db, role.id)
    if in_use:
        raise ConflictError(
            "The role is assigned to users", code="role_in_use", details={"user_count": in_use}
        )
    await db.delete(role)
    try:
        await db.flush()
    except IntegrityError as exc:  # assigned concurrently (user_roles FK is RESTRICT)
        raise ConflictError("The role is assigned to users", code="role_in_use") from exc


# ================================================================ business units
_legal_entities = table("legal_entities", column("id"), column("status"))


async def _check_default_legal_entity(db: AsyncSession, legal_entity_id: uuid.UUID) -> None:
    """Epic 14 (FR-76): a business unit's default Legal Entity must exist (RLS: in this tenant) and be active."""
    status = (
        await db.execute(select(_legal_entities.c.status).where(_legal_entities.c.id == legal_entity_id))
    ).scalar_one_or_none()
    if status != "active":
        raise invalid_reference("default_legal_entity_id", [legal_entity_id])


def _bu_exists() -> ConflictError:
    return ConflictError("A business unit with this name already exists", code="business_unit_exists")


async def _get_bu(db: AsyncSession, bu_id: uuid.UUID, *, for_update: bool = False) -> BusinessUnit:
    bu = await db.get(BusinessUnit, bu_id, with_for_update=for_update)
    if bu is None:
        raise NotFound("Business unit not found")
    return bu


async def list_business_units(
    ctx: RequestContext, db: AsyncSession, params: PageParams
) -> Page[BusinessUnitOut]:
    service.require_user(ctx)
    page = await paginate(db, select(BusinessUnit).order_by(BusinessUnit.name, BusinessUnit.id), params)
    return Page[BusinessUnitOut](
        items=[BusinessUnitOut.model_validate(b) for b in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def get_business_unit(ctx: RequestContext, db: AsyncSession, bu_id: uuid.UUID) -> BusinessUnitOut:
    service.require_user(ctx)
    return BusinessUnitOut.model_validate(await _get_bu(db, bu_id))


async def create_business_unit(
    ctx: RequestContext, db: AsyncSession, data: BusinessUnitCreate
) -> BusinessUnitOut:
    principal = service.require_user(ctx)
    assert principal.tenant_id is not None
    if (await db.execute(select(BusinessUnit.id).where(BusinessUnit.name == data.name))).first():
        raise _bu_exists()
    if data.default_legal_entity_id is not None:
        await _check_default_legal_entity(db, data.default_legal_entity_id)
    bu = BusinessUnit(
        tenant_id=principal.tenant_id,
        name=data.name,
        description=data.description,
        default_legal_entity_id=data.default_legal_entity_id,
    )
    db.add(bu)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _conflict_on(exc, BU_NAME_CONSTRAINT, _bu_exists()) from exc
    await db.refresh(bu)
    return BusinessUnitOut.model_validate(bu)


async def update_business_unit(
    ctx: RequestContext, db: AsyncSession, bu_id: uuid.UUID, data: BusinessUnitUpdate
) -> BusinessUnitOut:
    service.require_user(ctx)
    bu = await _get_bu(db, bu_id, for_update=True)
    sent = data.model_fields_set
    if "name" in sent and data.name is not None and data.name != bu.name:
        clash = await db.execute(
            select(BusinessUnit.id).where(BusinessUnit.name == data.name, BusinessUnit.id != bu.id)
        )
        if clash.first():
            raise _bu_exists()
        bu.name = data.name
    if "description" in sent:
        bu.description = data.description
    if "default_legal_entity_id" in sent and data.default_legal_entity_id != bu.default_legal_entity_id:
        if data.default_legal_entity_id is not None:
            await _check_default_legal_entity(db, data.default_legal_entity_id)
        bu.default_legal_entity_id = data.default_legal_entity_id
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _conflict_on(exc, BU_NAME_CONSTRAINT, _bu_exists()) from exc
    await db.refresh(bu)
    return BusinessUnitOut.model_validate(bu)


async def delete_business_unit(ctx: RequestContext, db: AsyncSession, bu_id: uuid.UUID) -> None:
    service.require_user(ctx)
    bu = await _get_bu(db, bu_id, for_update=True)
    users = (
        await db.execute(select(func.count()).select_from(User).where(User.business_unit_id == bu.id))
    ).scalar_one()
    teams = (
        await db.execute(select(func.count()).select_from(Team).where(Team.business_unit_id == bu.id))
    ).scalar_one()
    if users or teams:
        raise ConflictError(
            "The business unit still has users or teams",
            code="business_unit_in_use",
            details={"user_count": int(users), "team_count": int(teams)},
        )
    await db.delete(bu)
    try:
        await db.flush()
    except IntegrityError as exc:  # referenced concurrently (FKs are RESTRICT)
        raise ConflictError(
            "The business unit still has users or teams", code="business_unit_in_use"
        ) from exc


# ================================================================ teams
def _team_exists() -> ConflictError:
    return ConflictError("A team with this name already exists", code="team_exists")


async def _get_team(db: AsyncSession, team_id: uuid.UUID, *, for_update: bool = False) -> Team:
    team = await db.get(Team, team_id, with_for_update=for_update)
    if team is None:
        raise NotFound("Team not found")
    return team


async def _member_counts(db: AsyncSession, team_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not team_ids:
        return {}
    rows = await db.execute(
        select(TeamMember.team_id, func.count())
        .where(TeamMember.team_id.in_(team_ids))
        .group_by(TeamMember.team_id)
    )
    return {team_id: int(n) for team_id, n in rows.tuples()}


def _team_out(team: Team, member_count: int) -> TeamOut:
    return TeamOut(
        id=team.id,
        name=team.name,
        description=team.description,
        business_unit_id=team.business_unit_id,
        member_count=member_count,
        created_at=team.created_at,
        updated_at=team.updated_at,
    )


async def _team_detail(db: AsyncSession, team: Team, principal: Principal) -> TeamDetailOut:
    """Team with members; member email / active flag only for principals holding ``user:read``."""
    show_user = principal.has("user:read")
    rows = await db.execute(
        select(User.id, User.name, User.email, User.is_active, TeamMember.is_lead)
        .join(TeamMember, TeamMember.user_id == User.id)
        .where(TeamMember.team_id == team.id)
        .order_by(TeamMember.is_lead.desc(), User.name, User.id)
    )
    members = [
        TeamMemberOut(
            user_id=uid,
            name=name,
            email=email if show_user else None,
            is_active=active if show_user else None,
            is_lead=lead,
        )
        for uid, name, email, active, lead in rows.tuples()
    ]
    return TeamDetailOut(**_team_out(team, len(members)).model_dump(), members=members)


async def list_teams(
    ctx: RequestContext, db: AsyncSession, params: PageParams, business_unit_id: uuid.UUID | None = None
) -> Page[TeamOut]:
    service.require_user(ctx)
    stmt = select(Team)
    if business_unit_id is not None:
        stmt = stmt.where(Team.business_unit_id == business_unit_id)
    page = await paginate(db, stmt.order_by(Team.name, Team.id), params)
    counts = await _member_counts(db, [t.id for t in page.items])
    return Page[TeamOut](
        items=[_team_out(t, counts.get(t.id, 0)) for t in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def get_team(ctx: RequestContext, db: AsyncSession, team_id: uuid.UUID) -> TeamDetailOut:
    principal = service.require_user(ctx)
    return await _team_detail(db, await _get_team(db, team_id), principal)


async def create_team(ctx: RequestContext, db: AsyncSession, data: TeamCreate) -> TeamDetailOut:
    principal = service.require_user(ctx)
    assert principal.tenant_id is not None
    await _check_references(db, business_unit_id=data.business_unit_id)
    if (await db.execute(select(Team.id).where(Team.name == data.name))).first():
        raise _team_exists()
    team = Team(
        tenant_id=principal.tenant_id,
        name=data.name,
        description=data.description,
        business_unit_id=data.business_unit_id,
    )
    db.add(team)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _conflict_on(exc, TEAM_NAME_CONSTRAINT, _team_exists()) from exc
    await db.refresh(team)
    return await _team_detail(db, team, principal)


async def update_team(
    ctx: RequestContext, db: AsyncSession, team_id: uuid.UUID, data: TeamUpdate
) -> TeamDetailOut:
    principal = service.require_user(ctx)
    team = await _get_team(db, team_id, for_update=True)
    sent = data.model_fields_set
    if "business_unit_id" in sent:
        await _check_references(db, business_unit_id=data.business_unit_id)
        team.business_unit_id = data.business_unit_id
    if "name" in sent and data.name is not None and data.name != team.name:
        if (await db.execute(select(Team.id).where(Team.name == data.name, Team.id != team.id))).first():
            raise _team_exists()
        team.name = data.name
    if "description" in sent:
        team.description = data.description
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _conflict_on(exc, TEAM_NAME_CONSTRAINT, _team_exists()) from exc
    await db.refresh(team)
    return await _team_detail(db, team, principal)


async def delete_team(ctx: RequestContext, db: AsyncSession, team_id: uuid.UUID) -> None:
    """Delete a team; its memberships go with it."""
    service.require_user(ctx)
    team = await _get_team(db, team_id, for_update=True)
    # ORM deletes (not a bulk DELETE) so every removed membership is audited.
    for member in (await db.execute(select(TeamMember).where(TeamMember.team_id == team.id))).scalars():
        await db.delete(member)
    await db.flush()
    await db.delete(team)
    await db.flush()


async def set_team_member(
    ctx: RequestContext, db: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID, data: TeamMemberSet
) -> TeamDetailOut:
    """Add ``user_id`` to the team or update their lead flag (idempotent)."""
    principal = service.require_user(ctx)
    assert principal.tenant_id is not None
    team = await _get_team(db, team_id, for_update=True)
    await _get_user(db, user_id)
    if user_id == principal.id:
        raise _self_scope_change()
    member = await db.get(TeamMember, (team.id, user_id))
    if member is None:
        db.add(
            TeamMember(tenant_id=principal.tenant_id, team_id=team.id, user_id=user_id, is_lead=data.is_lead)
        )
    else:
        member.is_lead = data.is_lead
    await db.flush()
    return await _team_detail(db, team, principal)


async def remove_team_member(
    ctx: RequestContext, db: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    service.require_user(ctx)
    team = await _get_team(db, team_id, for_update=True)
    member = await db.get(TeamMember, (team.id, user_id), with_for_update=True)
    if member is None:
        raise NotFound("The user is not a member of this team")
    await db.delete(member)
    await db.flush()
