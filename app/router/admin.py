"""Tenant administration endpoints (story 1.5): ``/api/v1/admin/...`` and ``/api/v1/permissions``.

Every route authenticates the user and checks its permission via ``require(...)``; the DB session is the
user's RLS-scoped tenant session. Logic lives in ``identity.rbac``.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status

from app.core import audit, rate_limit
from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.identity import (
    AdminUserOut,
    BusinessUnitCreate,
    BusinessUnitOut,
    BusinessUnitUpdate,
    InvitationAdminOut,
    ModuleAppOut,
    PermissionOut,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    TeamCreate,
    TeamDetailOut,
    TeamMemberSet,
    TeamOut,
    TeamUpdate,
    UserFilters,
    UserInvite,
    UserUpdate,
)
from app.schemas.mail_settings import MailSettingsOut, MailSettingsUpdate, MailTestRequest, MailTestResult
from app.services import mail_settings_service as mail_settings
from app.services import rbac_service as rbac

admin_router = APIRouter(prefix="/admin", tags=["admin"])
permissions_router = APIRouter(tags=["admin"])


UserRead = Annotated[Principal, Depends(require("user:read"))]
UserManage = Annotated[Principal, Depends(require("user:manage"))]
RoleRead = Annotated[Principal, Depends(require("role:read"))]
RoleManage = Annotated[Principal, Depends(require("role:manage"))]
TeamRead = Annotated[Principal, Depends(require("team:read"))]
TeamManage = Annotated[Principal, Depends(require("team:manage"))]
BURead = Annotated[Principal, Depends(require("business_unit:read"))]
BUManage = Annotated[Principal, Depends(require("business_unit:manage"))]
SettingsRead = Annotated[Principal, Depends(require("settings:read"))]
SettingsManage = Annotated[Principal, Depends(require("settings:manage"))]
AuditRead = Annotated[Principal, Depends(require("audit:read"))]
AuditFilterParams = Annotated[audit.AuditFilters, Depends()]
Paging = Annotated[PageParams, Depends()]


# ================================================================ permissions catalog
@permissions_router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(_: RoleRead) -> list[PermissionOut]:
    return rbac.permission_catalog()


# ================================================================ module capability catalog (unified RBAC)
@admin_router.get("/module-capabilities", response_model=list[ModuleAppOut])
async def list_module_capabilities(_: RoleRead) -> list[ModuleAppOut]:
    """The per-module capability catalog (Croar / Feedback IQ / Job Portal) for the role editor."""
    return rbac.module_capability_catalog()


# ================================================================ users
@admin_router.get("/users", response_model=Page[AdminUserOut])
async def list_users(
    _: UserRead, db: TenantDBSessionDep, filters: Annotated[UserFilters, Depends()], params: Paging
) -> Page[AdminUserOut]:
    return await rbac.list_users(get_context(), db, filters, params)


@admin_router.post("/users/invite", response_model=InvitationAdminOut, status_code=status.HTTP_201_CREATED)
async def invite_user(
    body: UserInvite, _: UserManage, db: TenantDBSessionDep, background: BackgroundTasks
) -> InvitationAdminOut:
    return await rbac.invite_user(get_context(), db, body, background)


@admin_router.get("/users/{user_id}", response_model=AdminUserOut)
async def get_user(user_id: uuid.UUID, _: UserRead, db: TenantDBSessionDep) -> AdminUserOut:
    return await rbac.get_user(get_context(), db, user_id)


@admin_router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: uuid.UUID, body: UserUpdate, _: UserManage, db: TenantDBSessionDep
) -> AdminUserOut:
    return await rbac.update_user(get_context(), db, user_id, body)


@admin_router.post("/users/{user_id}/deactivate", response_model=AdminUserOut)
async def deactivate_user(user_id: uuid.UUID, _: UserManage, db: TenantDBSessionDep) -> AdminUserOut:
    return await rbac.deactivate_user(get_context(), db, user_id)


@admin_router.post("/users/{user_id}/reactivate", response_model=AdminUserOut)
async def reactivate_user(user_id: uuid.UUID, _: UserManage, db: TenantDBSessionDep) -> AdminUserOut:
    return await rbac.reactivate_user(get_context(), db, user_id)


# ================================================================ invitations
@admin_router.get("/invitations", response_model=Page[InvitationAdminOut])
async def list_invitations(_: UserRead, db: TenantDBSessionDep, params: Paging) -> Page[InvitationAdminOut]:
    return await rbac.list_invitations(get_context(), db, params)


@admin_router.post("/invitations/{invitation_id}/resend", response_model=InvitationAdminOut)
async def resend_invitation(
    invitation_id: uuid.UUID, _: UserManage, db: TenantDBSessionDep, background: BackgroundTasks
) -> InvitationAdminOut:
    return await rbac.resend_invitation(get_context(), db, invitation_id, background)


@admin_router.post("/invitations/{invitation_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(invitation_id: uuid.UUID, _: UserManage, db: TenantDBSessionDep) -> None:
    await rbac.revoke_invitation(get_context(), db, invitation_id)


# ================================================================ roles
@admin_router.get("/roles", response_model=list[RoleOut])
async def list_roles(_: RoleRead, db: TenantDBSessionDep) -> list[RoleOut]:
    return await rbac.list_roles(get_context(), db)


@admin_router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(body: RoleCreate, _: RoleManage, db: TenantDBSessionDep) -> RoleOut:
    return await rbac.create_role(get_context(), db, body)


@admin_router.get("/roles/{role_id}", response_model=RoleOut)
async def get_role(role_id: uuid.UUID, _: RoleRead, db: TenantDBSessionDep) -> RoleOut:
    return await rbac.get_role(get_context(), db, role_id)


@admin_router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(role_id: uuid.UUID, body: RoleUpdate, _: RoleManage, db: TenantDBSessionDep) -> RoleOut:
    return await rbac.update_role(get_context(), db, role_id, body)


@admin_router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(role_id: uuid.UUID, _: RoleManage, db: TenantDBSessionDep) -> None:
    await rbac.delete_role(get_context(), db, role_id)


# ================================================================ teams
@admin_router.get("/teams", response_model=Page[TeamOut])
async def list_teams(
    _: TeamRead,
    db: TenantDBSessionDep,
    params: Paging,
    business_unit_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[TeamOut]:
    return await rbac.list_teams(get_context(), db, params, business_unit_id)


@admin_router.post("/teams", response_model=TeamDetailOut, status_code=status.HTTP_201_CREATED)
async def create_team(body: TeamCreate, _: TeamManage, db: TenantDBSessionDep) -> TeamDetailOut:
    return await rbac.create_team(get_context(), db, body)


@admin_router.get("/teams/{team_id}", response_model=TeamDetailOut)
async def get_team(team_id: uuid.UUID, _: TeamRead, db: TenantDBSessionDep) -> TeamDetailOut:
    return await rbac.get_team(get_context(), db, team_id)


@admin_router.patch("/teams/{team_id}", response_model=TeamDetailOut)
async def update_team(
    team_id: uuid.UUID, body: TeamUpdate, _: TeamManage, db: TenantDBSessionDep
) -> TeamDetailOut:
    return await rbac.update_team(get_context(), db, team_id, body)


@admin_router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(team_id: uuid.UUID, _: TeamManage, db: TenantDBSessionDep) -> None:
    await rbac.delete_team(get_context(), db, team_id)


@admin_router.put("/teams/{team_id}/members/{user_id}", response_model=TeamDetailOut)
async def set_team_member(
    team_id: uuid.UUID, user_id: uuid.UUID, body: TeamMemberSet, _: TeamManage, db: TenantDBSessionDep
) -> TeamDetailOut:
    return await rbac.set_team_member(get_context(), db, team_id, user_id, body)


@admin_router.delete("/teams/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_team_member(
    team_id: uuid.UUID, user_id: uuid.UUID, _: TeamManage, db: TenantDBSessionDep
) -> None:
    await rbac.remove_team_member(get_context(), db, team_id, user_id)


# ================================================================ business units
@admin_router.get("/business-units", response_model=Page[BusinessUnitOut])
async def list_business_units(_: BURead, db: TenantDBSessionDep, params: Paging) -> Page[BusinessUnitOut]:
    return await rbac.list_business_units(get_context(), db, params)


@admin_router.post("/business-units", response_model=BusinessUnitOut, status_code=status.HTTP_201_CREATED)
async def create_business_unit(
    body: BusinessUnitCreate, _: BUManage, db: TenantDBSessionDep
) -> BusinessUnitOut:
    return await rbac.create_business_unit(get_context(), db, body)


@admin_router.get("/business-units/{bu_id}", response_model=BusinessUnitOut)
async def get_business_unit(bu_id: uuid.UUID, _: BURead, db: TenantDBSessionDep) -> BusinessUnitOut:
    return await rbac.get_business_unit(get_context(), db, bu_id)


@admin_router.patch("/business-units/{bu_id}", response_model=BusinessUnitOut)
async def update_business_unit(
    bu_id: uuid.UUID, body: BusinessUnitUpdate, _: BUManage, db: TenantDBSessionDep
) -> BusinessUnitOut:
    return await rbac.update_business_unit(get_context(), db, bu_id, body)


@admin_router.delete("/business-units/{bu_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_business_unit(bu_id: uuid.UUID, _: BUManage, db: TenantDBSessionDep) -> None:
    await rbac.delete_business_unit(get_context(), db, bu_id)


# ================================================================ audit log (story 1.6)
@admin_router.get("/audit-logs", response_model=Page[audit.AuditLogOut])
async def list_audit_logs(
    _: AuditRead, db: TenantDBSessionDep, filters: AuditFilterParams, params: Paging
) -> Page[audit.AuditLogOut]:
    return await audit.search(db, filters, params)


@admin_router.get(
    "/audit-logs/export.csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "CSV export (at most 50,000 rows)"}},
)
async def export_audit_logs(_: AuditRead, db: TenantDBSessionDep, filters: AuditFilterParams) -> Response:
    export = await audit.export_csv(db, filters)
    return Response(
        content=export.content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="audit-log.csv"',
            "X-Export-Rows": str(export.rows),
            "X-Export-Truncated": "true" if export.truncated else "false",
        },
    )


# ================================================================ outbound email (SMTP) settings
@admin_router.get("/mail-settings", response_model=MailSettingsOut)
async def get_mail_settings(_: SettingsRead, db: TenantDBSessionDep) -> MailSettingsOut:
    """Return the tenant's outbound email config; the password is masked (``password_set`` only)."""
    tenant_id = get_context().tenant_id
    assert tenant_id is not None
    return await mail_settings.get_mail_settings(db, tenant_id)


@admin_router.put("/mail-settings", response_model=MailSettingsOut)
async def put_mail_settings(
    body: MailSettingsUpdate, _: SettingsManage, db: TenantDBSessionDep
) -> MailSettingsOut:
    """Upsert the tenant's outbound email config. ``smtp_password`` is write-only (omit/null keeps it)."""
    return await mail_settings.upsert_mail_settings(get_context(), db, body)


@admin_router.post("/mail-settings/test", response_model=MailTestResult)
async def test_mail_settings(body: MailTestRequest, principal: SettingsManage) -> MailTestResult:
    """Send a REAL test email using the tenant's resolved mailer (rate-limited per tenant)."""
    tenant_id = principal.tenant_id
    assert tenant_id is not None
    await rate_limit.enforce("mail_test", str(tenant_id), 10, 3600)  # 10 test sends / hour / tenant
    return await mail_settings.send_test_email(tenant_id, body.to)


router = APIRouter()
router.include_router(permissions_router)
router.include_router(admin_router)
