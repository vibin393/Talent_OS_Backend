# ruff: file-ignore[line-too-long]
"""Developer workspace API (Epic 74, spec A4; ``/api/v1/developer``). Developer sessions only (``tos_developer`` +
CSRF on mutations; MFA mandatory for every workspace route). Org routes need a live membership of ``{org_id}`` /
``X-Partner-Org`` and the role of the §0.1 matrix. Nothing here accepts a tenant id from the request except the
selected org's own sandboxes."""

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response, status

from app.core.context import get_context
from app.core.security import (
    CSRF_COOKIE,
    USER_COOKIE,
    clear_cookie,
    csrf_token_for,
    set_csrf_cookie,
    set_session_cookie,
)
from app.dependencies.developer import DEV_COOKIE, DEV_MFA_COOKIE, DeveloperDep, verify_developer_csrf
from app.schemas.developer import (
    AppUsageOut,
    AuditRowOut,
    CloseAccountIn,
    HandoffIn,
    HandoffOut,
    HandoffResult,
    InvitationOut,
    InviteIn,
    LoginIn,
    LoginStepOut,
    MemberOut,
    MeOut,
    MfaCodeIn,
    MfaEnrollOut,
    MfaVerifyIn,
    OrgCreate,
    OrgOut,
    OrgUpdate,
    PasswordResetConfirmIn,
    PasswordResetRequestIn,
    PlaygroundExchangeIn,
    PlaygroundTokenIn,
    PlaygroundTokenOut,
    RecoveryCodesOut,
    RoleIn,
    RunOut,
    RunStartIn,
    SandboxCreate,
    SandboxInstallIn,
    SandboxInstallOut,
    SandboxOut,
    SandboxResetIn,
    SessionOut,
    SignupIn,
    SubmitIn,
    TokenIn,
    VerificationIn,
    VerificationOut,
)
from app.schemas.marketplace import ListingDraftIn, PartnerListingOut, VersionDraftIn, VersionOut
from app.schemas.oauth import AppCreate, AppCreated, AppOut, AppUpdate, SecretCreated
from app.services import developer_portal_service as dev

router = APIRouter(prefix="/developer", tags=["developer-portal"])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PENDING_MAX_AGE = 300


def _set_session(response: Response, token: str, max_age: int) -> str:
    set_session_cookie(response, DEV_COOKIE, token, max_age)
    csrf = csrf_token_for(token)
    set_csrf_cookie(response, csrf, max_age)
    clear_cookie(response, DEV_MFA_COOKIE)
    response.headers.update(NO_STORE)
    return csrf


# ================================================================ auth (public, rate-limited)
@router.post("/auth/signup", status_code=status.HTTP_202_ACCEPTED)
async def signup(body: SignupIn) -> dict[str, str]:
    await dev.signup(body, ip=get_context().ip)
    return {"status": "check_email"}


@router.post("/auth/verify-email", status_code=status.HTTP_204_NO_CONTENT)
async def verify_email(body: TokenIn) -> None:
    await dev.verify_email(body.token)


@router.post("/auth/login", response_model=LoginStepOut)
async def login(body: LoginIn, response: Response) -> LoginStepOut:
    ctx = get_context()
    step, pending = await dev.login(body, ip=ctx.ip, user_agent=ctx.user_agent)
    set_session_cookie(response, DEV_MFA_COOKIE, pending.token, PENDING_MAX_AGE)
    set_csrf_cookie(response, csrf_token_for(pending.token), PENDING_MAX_AGE)
    response.headers.update(NO_STORE)
    return LoginStepOut(step=step)  # type: ignore[arg-type]


@router.post("/auth/mfa/enroll/start", response_model=MfaEnrollOut)
async def mfa_enroll_start(request: Request, response: Response) -> MfaEnrollOut:
    verify_developer_csrf(request)
    response.headers.update(NO_STORE)
    return await dev.mfa_enroll_start(request.cookies.get(DEV_MFA_COOKIE))


@router.post("/auth/mfa/enroll/confirm", response_model=RecoveryCodesOut)
async def mfa_enroll_confirm(body: MfaCodeIn, request: Request, response: Response) -> RecoveryCodesOut:
    verify_developer_csrf(request)
    ctx = get_context()
    codes, issued, _ = await dev.mfa_enroll_confirm(request.cookies.get(DEV_MFA_COOKIE), body.code, ip=ctx.ip, user_agent=ctx.user_agent)
    _set_session(response, issued.token, issued.max_age)
    return codes


@router.post("/auth/mfa/verify", response_model=SessionOut)
async def mfa_verify(body: MfaVerifyIn, request: Request, response: Response) -> SessionOut:
    verify_developer_csrf(request)
    ctx = get_context()
    issued, account_id = await dev.mfa_verify(request.cookies.get(DEV_MFA_COOKIE), body, ip=ctx.ip, user_agent=ctx.user_agent)
    csrf = _set_session(response, issued.token, issued.max_age)
    return SessionOut(account=await dev.me(account_id), csrf_token=csrf)


@router.post("/auth/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
async def password_reset_request(body: PasswordResetRequestIn) -> dict[str, str]:
    await dev.request_password_reset(str(body.email))
    return {"status": "check_email"}


@router.post("/auth/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def password_reset_confirm(body: PasswordResetConfirmIn) -> None:
    await dev.reset_password(body.token, body.password)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(dctx: DeveloperDep, response: Response, everywhere: bool = False) -> None:
    await dev.logout(dctx.developer_id, dctx.session_id, everywhere=everywhere)
    clear_cookie(response, DEV_COOKIE)
    clear_cookie(response, CSRF_COOKIE, httponly=False)


@router.get("/csrf")
async def csrf(dctx: DeveloperDep, request: Request, response: Response) -> dict[str, str]:  # ruff: ignore[unused-function-argument]
    token = csrf_token_for(request.cookies.get(DEV_COOKIE) or "")
    set_csrf_cookie(response, token, 12 * 3600)
    return {"csrf_token": token}


@router.get("/me", response_model=MeOut)
async def get_me(dctx: DeveloperDep) -> MeOut:
    return await dev.me(dctx.developer_id)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(body: CloseAccountIn, dctx: DeveloperDep, response: Response) -> None:
    await dev.close_account(dctx.developer_id, password=body.password)
    clear_cookie(response, DEV_COOKIE)


# ================================================================ orgs
@router.get("/orgs", response_model=list[OrgOut])
async def list_orgs(dctx: DeveloperDep) -> list[OrgOut]:
    return (await dev.me(dctx.developer_id)).orgs


@router.post("/orgs", response_model=OrgOut, status_code=status.HTTP_201_CREATED)
async def create_org(body: OrgCreate, dctx: DeveloperDep) -> OrgOut:
    return await dev.create_org(dctx, body)


@router.get("/orgs/{org_id}", response_model=OrgOut)
async def get_org(org_id: uuid.UUID, dctx: DeveloperDep) -> OrgOut:
    return await dev.get_org(dctx, org_id)


@router.patch("/orgs/{org_id}", response_model=OrgOut)
async def update_org(org_id: uuid.UUID, body: OrgUpdate, dctx: DeveloperDep) -> OrgOut:
    return await dev.update_org(dctx, org_id, body)


@router.get("/orgs/{org_id}/members", response_model=list[MemberOut])
async def list_members(org_id: uuid.UUID, dctx: DeveloperDep) -> list[MemberOut]:
    return await dev.members(dctx, org_id)


@router.patch("/orgs/{org_id}/members/{account_id}", response_model=MemberOut)
async def set_role(org_id: uuid.UUID, account_id: uuid.UUID, body: RoleIn, dctx: DeveloperDep) -> MemberOut:
    return await dev.set_member_role(dctx, org_id, account_id, body.role)


@router.delete("/orgs/{org_id}/members/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(org_id: uuid.UUID, account_id: uuid.UUID, dctx: DeveloperDep) -> None:
    await dev.remove_member(dctx, org_id, account_id)


@router.post("/orgs/{org_id}/invitations", response_model=InvitationOut, status_code=status.HTTP_201_CREATED)
async def invite(org_id: uuid.UUID, body: InviteIn, dctx: DeveloperDep) -> InvitationOut:
    return await dev.invite(dctx, org_id, body)


@router.post("/invitations/accept", response_model=OrgOut)
async def accept_invitation(body: TokenIn, dctx: DeveloperDep) -> OrgOut:
    return await dev.accept_invitation(dctx, body.token)


@router.get("/orgs/{org_id}/audit", response_model=list[AuditRowOut])
async def org_audit(org_id: uuid.UUID, dctx: DeveloperDep, action: str | None = None, account_id: uuid.UUID | None = None) -> list[AuditRowOut]:
    return await dev.org_audit(dctx, org_id, action=action, account_id=account_id)


@router.get("/orgs/{org_id}/verification", response_model=VerificationOut)
async def get_verification(org_id: uuid.UUID, dctx: DeveloperDep) -> VerificationOut:
    return await dev.get_verification(dctx, org_id)


@router.put("/orgs/{org_id}/verification", response_model=VerificationOut)
async def save_verification(org_id: uuid.UUID, body: VerificationIn, dctx: DeveloperDep) -> VerificationOut:
    return await dev.save_verification(dctx, org_id, body)


@router.post("/orgs/{org_id}/verification/submit", response_model=VerificationOut)
async def submit_verification(org_id: uuid.UUID, dctx: DeveloperDep) -> VerificationOut:
    return await dev.submit_verification(dctx, org_id)


# ================================================================ apps
@router.get("/orgs/{org_id}/apps", response_model=list[AppOut])
async def list_apps(org_id: uuid.UUID, dctx: DeveloperDep) -> list[AppOut]:
    return await dev.list_org_apps(dctx, org_id)


@router.post("/orgs/{org_id}/apps", response_model=AppCreated, status_code=status.HTTP_201_CREATED)
async def create_app(org_id: uuid.UUID, body: AppCreate, dctx: DeveloperDep, response: Response) -> AppCreated:
    response.headers.update(NO_STORE)
    return await dev.create_org_app(dctx, org_id, body)


@router.get("/apps/{app_id}", response_model=AppOut)
async def get_app(app_id: uuid.UUID, dctx: DeveloperDep) -> AppOut:
    return await dev.get_org_app(dctx, app_id)


@router.get("/apps/{app_id}/secrets")
async def list_secrets(app_id: uuid.UUID, dctx: DeveloperDep) -> list[dict[str, Any]]:
    return await dev.list_org_app_secrets(dctx, app_id)


@router.patch("/apps/{app_id}", response_model=AppOut)
async def update_app(app_id: uuid.UUID, body: AppUpdate, dctx: DeveloperDep) -> AppOut:
    return await dev.update_org_app(dctx, app_id, body)


@router.post("/apps/{app_id}/secrets/rotate", response_model=SecretCreated)
async def rotate_secret(app_id: uuid.UUID, dctx: DeveloperDep, response: Response) -> SecretCreated:
    response.headers.update(NO_STORE)
    return await dev.rotate_org_app_secret(dctx, app_id)


@router.post("/apps/{app_id}/secrets/{secret_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_secret(app_id: uuid.UUID, secret_id: uuid.UUID, dctx: DeveloperDep, confirm_last: bool = False) -> None:
    await dev.revoke_org_app_secret(dctx, app_id, secret_id, confirm_last=confirm_last)


@router.post("/apps/{app_id}/retire", response_model=AppOut)
async def retire_app(app_id: uuid.UUID, body: dict[str, str], dctx: DeveloperDep) -> AppOut:
    return await dev.retire_org_app(dctx, app_id, str(body.get("reason") or "Retired by the developer"))


# ================================================================ submissions
@router.post("/apps/{app_id}/listing", response_model=PartnerListingOut, status_code=status.HTTP_201_CREATED)
async def listing_draft(app_id: uuid.UUID, body: ListingDraftIn, dctx: DeveloperDep) -> PartnerListingOut:
    return await dev.listing_draft(dctx, app_id, body)


@router.put("/listings/{listing_id}/versions", response_model=VersionOut)
async def save_version(listing_id: uuid.UUID, body: VersionDraftIn, dctx: DeveloperDep) -> VersionOut:
    return await dev.save_version(dctx, listing_id, body)


@router.post("/versions/{version_id}/submit", response_model=VersionOut)
async def submit_version(version_id: uuid.UUID, body: SubmitIn, dctx: DeveloperDep) -> VersionOut:
    return await dev.submit(dctx, version_id, conformance_run_id=body.conformance_run_id)


@router.post("/versions/{version_id}/withdraw", response_model=VersionOut)
async def withdraw_version(version_id: uuid.UUID, dctx: DeveloperDep) -> VersionOut:
    return await dev.withdraw(dctx, version_id)


@router.get("/orgs/{org_id}/submissions")
async def submissions(org_id: uuid.UUID, dctx: DeveloperDep) -> list[dict[str, Any]]:
    return await dev.submissions(dctx, org_id)


# ================================================================ sandboxes + playground (partner owner)
@router.get("/orgs/{org_id}/sandboxes", response_model=list[SandboxOut])
async def list_sandboxes(org_id: uuid.UUID, dctx: DeveloperDep) -> list[SandboxOut]:  # ruff: ignore[unused-function-argument]
    return await dev.list_sandboxes(await dev.partner_owner(dctx))


@router.post("/orgs/{org_id}/sandboxes", response_model=SandboxOut, status_code=status.HTTP_201_CREATED)
async def create_sandbox(org_id: uuid.UUID, body: SandboxCreate, dctx: DeveloperDep) -> SandboxOut:  # ruff: ignore[unused-function-argument]
    return await dev.create_sandbox(await dev.partner_owner(dctx), body)


@router.post("/sandboxes/{sandbox_id}/reset", response_model=SandboxOut)
async def reset_sandbox(sandbox_id: uuid.UUID, body: SandboxResetIn, dctx: DeveloperDep) -> SandboxOut:
    return await dev.reset_sandbox(await dev.partner_owner(dctx), sandbox_id, reinstall_apps=body.reinstall_apps)


@router.delete("/sandboxes/{sandbox_id}", response_model=SandboxOut)
async def delete_sandbox(sandbox_id: uuid.UUID, dctx: DeveloperDep) -> SandboxOut:
    return await dev.delete_sandbox(await dev.partner_owner(dctx), sandbox_id)


@router.post("/sandboxes/{sandbox_id}/open", response_model=HandoffOut)
async def open_sandbox(sandbox_id: uuid.UUID, dctx: DeveloperDep, response: Response) -> HandoffOut:
    response.headers.update(NO_STORE)
    return await dev.open_sandbox(await dev.partner_owner(dctx), sandbox_id)


@router.post("/sandboxes/{sandbox_id}/installations", response_model=SandboxInstallOut, status_code=status.HTTP_201_CREATED)
async def install_in_sandbox(sandbox_id: uuid.UUID, body: SandboxInstallIn, dctx: DeveloperDep) -> SandboxInstallOut:
    return await dev.install_in_sandbox(await dev.partner_owner(dctx), sandbox_id, body)


@router.post("/sandbox-handoff", response_model=HandoffResult)
async def sandbox_handoff(body: HandoffIn, request: Request, response: Response) -> HandoffResult:
    """One-time token -> a normal ``tos_session`` for the developer's sandbox user (no developer session needed)."""
    account_id = None
    if request.cookies.get(DEV_COOKIE):
        from app.dependencies.developer import require_developer

        try:
            account_id = (await require_developer(request)).developer_id
        except Exception:
            account_id = None
    ctx = get_context()
    token, csrf, max_age = await dev.consume_handoff(body.t, account_id=account_id, ip=ctx.ip, user_agent=ctx.user_agent)
    set_session_cookie(response, USER_COOKIE, token, max_age)
    set_csrf_cookie(response, csrf, max_age)
    response.headers.update(NO_STORE)
    return HandoffResult(redirect="/dashboard")


@router.post("/playground/token", response_model=PlaygroundTokenOut)
async def playground_token(body: PlaygroundTokenIn, dctx: DeveloperDep, response: Response) -> PlaygroundTokenOut:
    response.headers.update(NO_STORE)
    return await dev.playground_token(await dev.partner_owner(dctx), body)


@router.post("/playground/exchange", response_model=PlaygroundTokenOut)
async def playground_exchange(body: PlaygroundExchangeIn, dctx: DeveloperDep, response: Response) -> PlaygroundTokenOut:
    response.headers.update(NO_STORE)
    return await dev.playground_exchange(await dev.partner_owner(dctx), body)


# ================================================================ conformance + usage
@router.post("/apps/{app_id}/conformance-runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def start_run(app_id: uuid.UUID, body: RunStartIn, dctx: DeveloperDep) -> RunOut:
    return await dev.start_run(await dev.partner_owner(dctx), app_id, body.sandbox_id)


@router.get("/apps/{app_id}/conformance-runs", response_model=list[RunOut])
async def list_runs(app_id: uuid.UUID, dctx: DeveloperDep) -> list[RunOut]:
    return await dev.list_runs(await dev.partner_owner(dctx), app_id)


@router.post("/conformance-runs/{run_id}/finish", response_model=RunOut)
async def finish_run(run_id: uuid.UUID, dctx: DeveloperDep) -> RunOut:
    return await dev.finish_run(await dev.partner_owner(dctx), run_id)


@router.get("/conformance-runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, dctx: DeveloperDep) -> RunOut:
    return await dev.get_run(await dev.partner_owner(dctx), run_id)


@router.get("/conformance-runs/{run_id}/report")
async def download_report(run_id: uuid.UUID, dctx: DeveloperDep) -> dict[str, Any]:
    return await dev.download_report(await dev.partner_owner(dctx), run_id)


@router.get("/apps/{app_id}/usage", response_model=AppUsageOut)
async def app_usage(
    app_id: uuid.UUID, dctx: DeveloperDep, date_from: Annotated[dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[dt.date | None, Query(alias="to")] = None,
) -> AppUsageOut:
    end = date_to or dt.date.today()
    return await dev.app_usage(await dev.partner_owner(dctx), app_id, date_from or end - dt.timedelta(days=29), end)
