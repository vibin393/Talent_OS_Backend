"""Client Portal (``/api/v1/client-portal``). Epic 13 ``/me``; Epic 14 owns this file: sign-in (magic link +
6-digit code, optional / enforced TOTP), ``/csrf`` re-issue and logout. Epic 18 adds
``client_portal_<domain>.py`` routers under the same prefix. Client Users never reach internal routes.

Every JSON response is scanned for FR-80 forbidden fields outside production (``portal_guard_route``).
"""

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Cookie, Request, Response, status

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.core.security import (
    CLIENT_COOKIE,
    MFA_COOKIE,
    clear_auth_cookies,
    clear_cookie,
    set_csrf_cookie,
    set_session_cookie,
)
from app.core.settings import get_settings
from app.dependencies.client import ClientDBSessionDep, ClientDep
from app.schemas.external_auth import ExternalMeOut
from app.schemas.external_auth_portal import (
    PortalAuthOut,
    PortalCodeVerifyIn,
    PortalLinkRequestIn,
    PortalLinkVerifyIn,
    PortalMfaIn,
    PortalStatusOut,
)
from app.services import external_auth_service as svc
from app.services.external_auth import signin

router = APIRouter(
    prefix="/client-portal", tags=["client-portal"], route_class=portal_guard_route("client_user")
)
auth_router = APIRouter(
    prefix="/client-portal/auth", tags=["client-portal"], route_class=portal_guard_route("client_user")
)

MfaChallengeCookie = Annotated[str | None, Cookie(alias=MFA_COOKIE)]


def apply_sign_in(response: Response, result: signin.SignInResult, cookie: str) -> PortalAuthOut:
    """Set the session + CSRF cookies (signed in) or the ``tos_mfa`` challenge cookie."""
    if result.session_token is not None and result.csrf_token is not None:
        set_session_cookie(response, cookie, result.session_token, result.max_age)
        set_csrf_cookie(response, result.csrf_token, result.max_age)
        clear_cookie(response, MFA_COOKIE)
    elif result.mfa_token is not None:
        set_session_cookie(response, MFA_COOKIE, result.mfa_token, result.mfa_max_age)
    return result.auth


@router.get("/me", response_model=ExternalMeOut)
async def me(_: ClientDep, db: ClientDBSessionDep) -> Any:
    return await svc.me(get_context(), db)


@router.get("/csrf", status_code=status.HTTP_204_NO_CONTENT)
async def reissue_csrf(_: ClientDep, request: Request, response: Response) -> None:
    """FR-82: re-issue ``tos_csrf`` for the Client User session (the client retries the failed call once)."""
    from app.core.security import CLIENT_COOKIE, csrf_token_from_request

    token = csrf_token_from_request(request, CLIENT_COOKIE)
    set_csrf_cookie(response, token, get_settings().external_session_absolute_hours * 3600)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(_: ClientDep, response: Response) -> None:
    await signin.logout(get_context())
    clear_auth_cookies(response, CLIENT_COOKIE)


# ---------------------------------------------------------------- public sign-in
@auth_router.post("/request-link", response_model=PortalStatusOut, status_code=status.HTTP_202_ACCEPTED)
async def request_link(body: PortalLinkRequestIn, background: BackgroundTasks) -> Any:
    return await signin.request_client_link(get_context(), body, background)


@auth_router.post("/verify-link", response_model=PortalAuthOut)
async def verify_link(body: PortalLinkVerifyIn, response: Response) -> Any:
    return apply_sign_in(response, await signin.verify_client_link(get_context(), body.token), CLIENT_COOKIE)


@auth_router.post("/verify-code", response_model=PortalAuthOut)
async def verify_code(body: PortalCodeVerifyIn, response: Response) -> Any:
    return apply_sign_in(response, await signin.verify_client_code(get_context(), body), CLIENT_COOKIE)


@auth_router.post("/mfa/verify", response_model=PortalAuthOut)
async def mfa_verify(body: PortalMfaIn, response: Response, tos_mfa: MfaChallengeCookie = None) -> Any:
    return apply_sign_in(
        response, await signin.verify_mfa(get_context(), "client_user", tos_mfa, body), CLIENT_COOKIE
    )
