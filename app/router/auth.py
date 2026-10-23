"""Identity HTTP endpoints for tenant users.

* ``/api/v1/auth/...``: tenant users -- invitation acceptance, sign-in/out, ``me``, password reset, MFA.
* ``/api/v1/tenant/security``: tenant security settings ("require MFA", story 1.4).

Platform-operator sign-in / tenant provisioning live in ``app.router.platform`` (``operator_router``);
tenant administration (``/api/v1/admin/...``, ``/api/v1/permissions``) in ``app.router.admin``.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Path, Request, Response, status

from app.core.context import get_context
from app.core.errors import Unauthorized, ValidationFailed
from app.core.permissions import require
from app.core.security import (
    MFA_COOKIE,
    USER_COOKIE,
    Principal,
    clear_auth_cookies,
    clear_cookie,
    set_csrf_cookie,
    set_session_cookie,
)
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.models.identity import User
from app.schemas.identity import (
    AuthOut,
    InvitationAccept,
    InvitationAcceptWithToken,
    InvitationOut,
    InvitationTokenIn,
    MfaCodeIn,
    MfaEnrollOut,
    MfaRequiredOut,
    MfaVerifyIn,
    PasswordResetConfirm,
    PasswordResetRequest,
    RecoveryCodesOut,
    TenantSecurityOut,
    TenantSecurityUpdate,
    UserLogin,
)
from app.services import access_service as access
from app.services import auth_service as service
from app.services import central_token
from app.services import mfa_service as mfa

auth_router = APIRouter(prefix="/auth", tags=["auth"])
tenant_router = APIRouter(prefix="/tenant", tags=["tenant"])

TokenPath = Annotated[str, Path(min_length=1, max_length=512)]
MfaChallengeCookie = Annotated[str | None, Cookie(alias=MFA_COOKIE)]


def _mfa_challenge(response: Response, result: service.MfaChallengeResult) -> MfaRequiredOut:
    set_session_cookie(response, MFA_COOKIE, result.token, result.max_age)
    return MfaRequiredOut()


# ================================================================ tenant users
def _sign_in(response: Response, result: service.UserAuthResult) -> AuthOut:
    set_session_cookie(response, USER_COOKIE, result.session_token, result.max_age)
    set_csrf_cookie(response, result.csrf_token, result.max_age)
    return result.auth


@auth_router.post("/invitations/lookup", response_model=InvitationOut)
async def lookup_invitation(body: InvitationTokenIn) -> InvitationOut:
    """Epic 12 (A10): body-token replacement of ``GET /invitations/{token}``."""
    return await service.get_invitation(get_context(), body.token)


@auth_router.post("/invitations/accept", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
async def accept_invitation_body(body: InvitationAcceptWithToken, response: Response) -> AuthOut:
    """Epic 12 (A10): body-token replacement of ``POST /invitations/{token}/accept``."""
    data = InvitationAccept(name=body.name, password=body.password)
    return _sign_in(response, await service.accept_invitation(get_context(), body.token, data))


@auth_router.get("/invitations/{token}", response_model=InvitationOut, deprecated=True)
async def get_invitation(token: TokenPath) -> InvitationOut:
    return await service.get_invitation(get_context(), token)


@auth_router.post(
    "/invitations/{token}/accept",
    response_model=AuthOut,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
)
async def accept_invitation(token: TokenPath, body: InvitationAccept, response: Response) -> AuthOut:
    return _sign_in(response, await service.accept_invitation(get_context(), token, body))


@auth_router.post("/login", response_model=AuthOut | MfaRequiredOut)
async def user_login(body: UserLogin, request: Request, response: Response) -> AuthOut | MfaRequiredOut:
    if body.tenant_slug is None:  # Epic 6.2: infer the tenant from the request subdomain
        body.tenant_slug = getattr(request.state, "tenant_slug", None)
        if body.tenant_slug is None:
            raise ValidationFailed("tenant_slug is required (or sign in on the tenant's subdomain)")
    result = await service.login_user(get_context(), body)
    if isinstance(result, service.MfaChallengeResult):
        return _mfa_challenge(response, result)
    await service.revoke_incoming_session(request, "user", USER_COOKIE)  # story 22.9: re-login revokes
    return _sign_in(response, result)


@auth_router.get("/jwks.json")
async def central_jwks() -> dict[str, object]:
    """Public JWKS so other apps validate central tenant-claim tokens offline (Epic 3 / AD-6)."""
    return central_token.jwks()


@auth_router.get("/token")
async def central_token_mint(principal: CurrentUserDep, db: TenantDBSessionDep) -> dict[str, object]:
    """Mint a short-lived tenant-claim JWT from the current session (hybrid model: the opaque
    ``tos_session`` stays the source of truth; this token is validated offline via JWKS). Carries the
    user's email/name so embedded apps can SSO the matching local user, and the user's effective
    ``module_caps`` (unified RBAC) so the other suite apps honor the central role grants offline."""
    if principal.tenant_id is None:
        raise Unauthorized()
    user = await db.get(User, principal.id)
    module_caps = await access.module_caps_for_user(db, principal.id)
    token = central_token.mint_central_token(
        user_id=principal.id,
        tenant_id=principal.tenant_id,
        email=getattr(user, "email", None),
        name=getattr(user, "name", None),
        module_caps=module_caps,
    )
    return {"access_token": token, "token_type": "Bearer", "expires_in": central_token.TTL_SECONDS}


@auth_router.post("/mfa/verify", response_model=AuthOut)
async def user_mfa_verify(
    body: MfaVerifyIn, request: Request, response: Response, tos_mfa: MfaChallengeCookie = None
) -> AuthOut:
    result = await mfa.verify_user_challenge(get_context(), tos_mfa, body)
    clear_cookie(response, MFA_COOKIE)
    await service.revoke_incoming_session(request, "user", USER_COOKIE)  # story 22.9: re-login revokes
    return _sign_in(response, result)


@auth_router.post("/mfa/enroll", response_model=MfaEnrollOut)
async def user_mfa_enroll(_: CurrentUserDep, db: TenantDBSessionDep) -> MfaEnrollOut:
    return await mfa.enroll_user(get_context(), db)


@auth_router.post("/mfa/enroll/confirm", response_model=RecoveryCodesOut)
async def user_mfa_confirm(body: MfaCodeIn, _: CurrentUserDep, db: TenantDBSessionDep) -> RecoveryCodesOut:
    return await mfa.confirm_user_enrollment(get_context(), db, body.code)


@auth_router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def user_mfa_disable(body: MfaCodeIn, _: CurrentUserDep, db: TenantDBSessionDep) -> None:
    await mfa.disable_user_mfa(get_context(), db, body.code)


@auth_router.post("/mfa/recovery-codes/regenerate", response_model=RecoveryCodesOut)
async def user_mfa_regenerate(body: MfaCodeIn, _: CurrentUserDep, db: TenantDBSessionDep) -> RecoveryCodesOut:
    return await mfa.regenerate_user_recovery_codes(get_context(), db, body.code)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def user_logout(_: CurrentUserDep, response: Response) -> None:
    await service.logout(get_context())
    clear_auth_cookies(response, USER_COOKIE)


@auth_router.get("/csrf", status_code=status.HTTP_204_NO_CONTENT)
async def reissue_csrf(_: CurrentUserDep, request: Request, response: Response) -> None:
    """Epic 14 (FR-82): re-issue ``tos_csrf`` for the user session; the frontend retries the failed call once.
    Story 22.9: the token is bound to the presented session (``csrf_token_for``)."""
    from app.core.security import csrf_token_from_request
    from app.core.settings import get_settings

    token = csrf_token_from_request(request, USER_COOKIE, "tos_support")
    set_csrf_cookie(response, token, get_settings().session_absolute_timeout_days * 86400)


@auth_router.get("/me", response_model=AuthOut)
async def user_me(_: CurrentUserDep, db: TenantDBSessionDep) -> AuthOut:
    return await service.get_me(get_context(), db)


@auth_router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
async def password_reset_request(body: PasswordResetRequest, background: BackgroundTasks) -> None:
    await service.request_password_reset(get_context(), body, background)


@auth_router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def password_reset_confirm(body: PasswordResetConfirm, response: Response) -> None:
    await service.confirm_password_reset(get_context(), body)
    # Every session of the user was revoked; drop this browser's cookies too.
    clear_auth_cookies(response, USER_COOKIE)


# ================================================================ tenant settings
@tenant_router.get("/security", response_model=TenantSecurityOut)
async def get_tenant_security(
    _: Annotated[Principal, Depends(require("settings:read"))], db: TenantDBSessionDep
) -> TenantSecurityOut:
    return await mfa.get_tenant_security(get_context(), db)


@tenant_router.patch("/security", response_model=TenantSecurityOut)
async def update_tenant_security(
    body: TenantSecurityUpdate,
    _: Annotated[Principal, Depends(require("settings:manage"))],
    db: TenantDBSessionDep,
) -> TenantSecurityOut:
    return await mfa.update_tenant_security(get_context(), db, body)
