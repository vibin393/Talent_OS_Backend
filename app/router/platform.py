"""Platform-operator endpoints of Epic 12 (``/api/v1/platform/*``), behind ``require_platform_operator`` + MFA.

``operator_router`` (same ``/platform`` prefix, mounted first): identity's operator sign-in, MFA and tenant
provisioning (``require_platform_operator``; MFA enrolment itself is reachable without MFA).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Response, status

from app.core.context import get_context
from app.core.database import DBSessionDep
from app.core.pagination import Page, PageParams
from app.core.security import (
    MFA_COOKIE,
    PLATFORM_COOKIE,
    Principal,
    clear_auth_cookies,
    clear_cookie,
    set_csrf_cookie,
    set_session_cookie,
)
from app.dependencies.platform import PlatformOperatorMfaDep
from app.schemas.agentic import PlatformKillSwitchIn
from app.schemas.ecosystem import PlatformSuspendIn, RateTierIn
from app.schemas.identity import (
    InvitationAdminOut,
    MfaCodeIn,
    MfaEnrollOut,
    MfaRequiredOut,
    MfaVerifyIn,
    OperatorLogin,
    OperatorOut,
    RecoveryCodesOut,
    TenantCreate,
    TenantOut,
)
from app.schemas.platform import (
    AIBudgetIn,
    IncidentCreate,
    IncidentNotificationOut,
    IncidentNotifyIn,
    IncidentOut,
    NoticeCreate,
    PlatformNoticeOut,
    SubProcessorIn,
    SubProcessorOut,
    SubProcessorUpdate,
    SupportGrantOut,
    SupportRequestCreate,
    SupportSessionOut,
    TenantLifecycleOut,
    TenantOffboardIn,
    TenantReasonIn,
)
from app.schemas.sandbox import SandboxCreate, SandboxOut
from app.services import (
    auth_service,
    mfa_service,
    platform_service,
    rbac_service,
    support_access_service,
    tenant_lifecycle_service,
)

operator_router = APIRouter(prefix="/platform", tags=["platform"])
PlatformOperatorDep = Annotated[Principal, Depends(auth_service.require_platform_operator)]
MfaChallengeCookie = Annotated[str | None, Cookie(alias=MFA_COOKIE)]


def _mfa_challenge(response: Response, result: auth_service.MfaChallengeResult) -> MfaRequiredOut:
    set_session_cookie(response, MFA_COOKIE, result.token, result.max_age)
    return MfaRequiredOut()


def _operator_signed_in(response: Response, result: auth_service.OperatorLoginResult) -> OperatorOut:
    set_session_cookie(response, PLATFORM_COOKIE, result.session_token, result.max_age)
    set_csrf_cookie(response, result.csrf_token, result.max_age)
    return result.operator


# ================================================================ platform operators
@operator_router.post("/auth/login", response_model=OperatorOut | MfaRequiredOut)
async def login(body: OperatorLogin, response: Response) -> OperatorOut | MfaRequiredOut:
    result = await auth_service.login_operator(get_context(), body)
    if isinstance(result, auth_service.MfaChallengeResult):
        return _mfa_challenge(response, result)
    return _operator_signed_in(response, result)


@operator_router.post("/auth/mfa/verify", response_model=OperatorOut)
async def operator_mfa_verify(
    body: MfaVerifyIn, response: Response, tos_mfa: MfaChallengeCookie = None
) -> OperatorOut:
    result = await mfa_service.verify_operator_challenge(get_context(), tos_mfa, body)
    clear_cookie(response, MFA_COOKIE)
    return _operator_signed_in(response, result)


@operator_router.post("/auth/mfa/enroll", response_model=MfaEnrollOut)
async def operator_mfa_enroll(_: PlatformOperatorDep, db: DBSessionDep) -> MfaEnrollOut:
    return await mfa_service.enroll_operator(get_context(), db)


@operator_router.post("/auth/mfa/enroll/confirm", response_model=RecoveryCodesOut)
async def operator_mfa_confirm(body: MfaCodeIn, _: PlatformOperatorDep, db: DBSessionDep) -> RecoveryCodesOut:
    return await mfa_service.confirm_operator_enrollment(get_context(), db, body.code)


@operator_router.post("/auth/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def operator_mfa_disable(body: MfaCodeIn, _: PlatformOperatorDep, db: DBSessionDep) -> None:
    await mfa_service.disable_operator_mfa(get_context(), db, body.code)


@operator_router.post("/auth/mfa/recovery-codes/regenerate", response_model=RecoveryCodesOut)
async def operator_mfa_regenerate(
    body: MfaCodeIn, _: PlatformOperatorDep, db: DBSessionDep
) -> RecoveryCodesOut:
    return await mfa_service.regenerate_operator_recovery_codes(get_context(), db, body.code)


@operator_router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(_: PlatformOperatorDep, response: Response) -> None:
    await auth_service.logout(get_context())
    clear_auth_cookies(response, PLATFORM_COOKIE)


@operator_router.get("/auth/me", response_model=OperatorOut)
async def me(_: PlatformOperatorDep, db: DBSessionDep) -> OperatorOut:
    return await auth_service.get_operator_profile(get_context(), db)


@operator_router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(body: TenantCreate, _: PlatformOperatorDep) -> TenantOut:
    return TenantOut.model_validate(await auth_service.create_tenant(get_context(), body))


@operator_router.get("/tenants", response_model=Page[TenantOut])
async def list_tenants(
    _: PlatformOperatorDep, db: DBSessionDep, params: Annotated[PageParams, Depends()]
) -> Page[TenantOut]:
    return await auth_service.list_tenants(get_context(), db, params)


@operator_router.get("/tenants/{tenant_id}", response_model=TenantOut)
async def get_tenant(tenant_id: uuid.UUID, _: PlatformOperatorDep, db: DBSessionDep) -> TenantOut:
    return TenantOut.model_validate(await auth_service.get_tenant(get_context(), db, tenant_id))


@operator_router.post("/tenants/{tenant_id}/invitations/resend", response_model=InvitationAdminOut)
async def resend_tenant_admin_invitation(
    tenant_id: uuid.UUID, _: PlatformOperatorDep, background: BackgroundTasks
) -> InvitationAdminOut:
    return await rbac_service.resend_tenant_admin_invitation(get_context(), tenant_id, background)


@operator_router.post("/tenants/{tenant_id}/invitations/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_tenant_admin_invitation(tenant_id: uuid.UUID, _: PlatformOperatorDep) -> None:
    await rbac_service.revoke_tenant_admin_invitation(get_context(), tenant_id)


router = APIRouter(prefix="/platform", tags=["platform"])
Paging = Annotated[PageParams, Depends()]


# ================================================================ tenants
@router.get("/tenants-overview", response_model=Page[TenantLifecycleOut])
async def tenants_overview(_: PlatformOperatorMfaDep, params: Paging) -> Page[TenantLifecycleOut]:
    return await platform_service.list_tenants(get_context(), params)


@router.get("/tenants/{tenant_id}/overview", response_model=TenantLifecycleOut)
async def tenant_overview(tenant_id: uuid.UUID, _: PlatformOperatorMfaDep) -> TenantLifecycleOut:
    return await platform_service.tenant_detail(get_context(), tenant_id)


@router.put("/tenants/{tenant_id}/ai-budget", response_model=TenantLifecycleOut)
async def set_ai_budget(
    tenant_id: uuid.UUID, body: AIBudgetIn, _: PlatformOperatorMfaDep
) -> TenantLifecycleOut:
    return await platform_service.set_ai_budget(get_context(), tenant_id, body.monthly_token_budget)


@router.post("/tenants/{tenant_id}/suspend", response_model=TenantLifecycleOut)
async def suspend(
    tenant_id: uuid.UUID, body: TenantReasonIn, _: PlatformOperatorMfaDep
) -> TenantLifecycleOut:
    return await tenant_lifecycle_service.transition(get_context(), tenant_id, "suspended", body.reason)


@router.post("/tenants/{tenant_id}/reactivate", response_model=TenantLifecycleOut)
async def reactivate(
    tenant_id: uuid.UUID, body: TenantReasonIn, _: PlatformOperatorMfaDep
) -> TenantLifecycleOut:
    return await tenant_lifecycle_service.transition(get_context(), tenant_id, "active", body.reason)


@router.post("/tenants/{tenant_id}/offboard", response_model=TenantLifecycleOut)
async def offboard(
    tenant_id: uuid.UUID, body: TenantOffboardIn, _: PlatformOperatorMfaDep
) -> TenantLifecycleOut:
    return await tenant_lifecycle_service.transition(
        get_context(),
        tenant_id,
        "offboarding",
        body.reason,
        contact_email=str(body.contact_email),
        confirm_slug=body.confirm_slug,
    )


@router.post("/tenants/{tenant_id}/cancel-offboard", response_model=TenantLifecycleOut)
async def cancel_offboard(
    tenant_id: uuid.UUID, body: TenantReasonIn, _: PlatformOperatorMfaDep
) -> TenantLifecycleOut:
    return await tenant_lifecycle_service.transition(get_context(), tenant_id, "active", body.reason)


# ================================================================ support access (operator side)
@router.post(
    "/tenants/{tenant_id}/support-requests",
    response_model=SupportGrantOut,
    status_code=status.HTTP_201_CREATED,
)
async def request_support(
    tenant_id: uuid.UUID, body: SupportRequestCreate, _: PlatformOperatorMfaDep
) -> SupportGrantOut:
    return await support_access_service.request_access(get_context(), tenant_id, body)


@router.get("/support-requests", response_model=list[SupportGrantOut])
async def list_support_requests(_: PlatformOperatorMfaDep, mine: bool = False) -> list[SupportGrantOut]:
    return await support_access_service.list_operator_requests(get_context(), mine_only=mine)


@router.post("/support-requests/{grant_id}/cancel", response_model=SupportGrantOut)
async def cancel_support_request(grant_id: uuid.UUID, _: PlatformOperatorMfaDep) -> SupportGrantOut:
    return await support_access_service.cancel(get_context(), grant_id)


@router.post("/support-requests/{grant_id}/session", response_model=SupportSessionOut)
async def start_support_session(
    grant_id: uuid.UUID, _: PlatformOperatorMfaDep, response: Response
) -> SupportSessionOut:
    out, token, max_age = await support_access_service.start_session(get_context(), grant_id)
    set_session_cookie(response, support_access_service.SUPPORT_COOKIE, token, max_age)
    return out


# ================================================================ sub-processors
@router.get("/sub-processors", response_model=list[SubProcessorOut])
async def list_sub_processors(_: PlatformOperatorMfaDep) -> list[SubProcessorOut]:
    return await platform_service.list_all_sub_processors(get_context())


@router.post("/sub-processors", response_model=SubProcessorOut, status_code=status.HTTP_201_CREATED)
async def create_sub_processor(body: SubProcessorIn, _: PlatformOperatorMfaDep) -> SubProcessorOut:
    return await platform_service.create_sub_processor(get_context(), body)


@router.patch("/sub-processors/{sub_id}", response_model=SubProcessorOut)
async def update_sub_processor(
    sub_id: uuid.UUID, body: SubProcessorUpdate, _: PlatformOperatorMfaDep
) -> SubProcessorOut:
    return await platform_service.update_sub_processor(get_context(), sub_id, body)


# ================================================================ notices
@router.get("/notices", response_model=list[PlatformNoticeOut])
async def list_notices(_: PlatformOperatorMfaDep) -> list[PlatformNoticeOut]:
    return await platform_service.list_all_notices(get_context())


@router.post("/notices", response_model=PlatformNoticeOut, status_code=status.HTTP_201_CREATED)
async def post_notice(body: NoticeCreate, _: PlatformOperatorMfaDep) -> PlatformNoticeOut:
    return await platform_service.post_notice(get_context(), body)


# ================================================================ incidents
@router.get("/incidents", response_model=Page[IncidentOut])
async def list_incidents(_: PlatformOperatorMfaDep, params: Paging) -> Page[IncidentOut]:
    return await platform_service.list_incidents(get_context(), params)


@router.post("/incidents", response_model=IncidentOut, status_code=status.HTTP_201_CREATED)
async def create_incident(body: IncidentCreate, _: PlatformOperatorMfaDep) -> IncidentOut:
    return await platform_service.create_incident(get_context(), body)


@router.post("/incidents/{incident_id}/notify", response_model=list[IncidentNotificationOut])
async def notify_incident(
    incident_id: uuid.UUID, body: IncidentNotifyIn, _: PlatformOperatorMfaDep
) -> list[IncidentNotificationOut]:
    return await platform_service.notify_incident(get_context(), incident_id, body.tenant_ids)


@router.get("/incidents/{incident_id}/notifications", response_model=list[IncidentNotificationOut])
async def incident_notifications(
    incident_id: uuid.UUID, _: PlatformOperatorMfaDep
) -> list[IncidentNotificationOut]:
    return await platform_service.incident_notifications(get_context(), incident_id)


# ---------------------------------------------------------------- Epic 53: AI agent kill switches (FR-271)
@router.post("/ai/kill-switch", status_code=status.HTTP_204_NO_CONTENT)
async def ai_kill_switch(body: PlatformKillSwitchIn, _: PlatformOperatorMfaDep) -> Response:
    from app.core.database import platform_session
    from app.services import agent_runtime_service

    async with platform_session() as db:
        await agent_runtime_service.platform_set_kill_switch(
            get_context(), db, tenant_id=body.tenant_id, on=body.on, reason=body.reason
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- Epic 66: Sandbox Tenants (FR-339, Story 66.9)
@router.post("/sandboxes", response_model=SandboxOut, status_code=status.HTTP_201_CREATED)
async def create_sandbox(body: SandboxCreate, _: PlatformOperatorMfaDep) -> SandboxOut:
    from app.services import sandbox_service

    return await sandbox_service.create_sandbox(get_context(), body)


# ---------------------------------------------------------------- Epic 66: ecosystem suspension + Rate Tiers (FR-338/340)
@router.post("/ecosystem/suspend", status_code=status.HTTP_204_NO_CONTENT)
async def ecosystem_suspend(body: PlatformSuspendIn, principal: PlatformOperatorMfaDep) -> Response:
    from app.services import ecosystem_service

    await ecosystem_service.platform_set(
        body.scope, tenant_id=body.tenant_id, app_id=body.app_id, on=body.on, reason=body.reason,
        operator_id=principal.id,
    )  # fmt: skip
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/tenants/{tenant_id}/rate-tier", status_code=status.HTTP_204_NO_CONTENT)
async def tenant_rate_tier(tenant_id: uuid.UUID, body: RateTierIn, principal: PlatformOperatorMfaDep) -> Response:
    from app.services import ecosystem_service

    await ecosystem_service.set_tenant_rate_tier(tenant_id, body.rate_tier, operator_id=principal.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
