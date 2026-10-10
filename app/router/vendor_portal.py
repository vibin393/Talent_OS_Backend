"""Vendor Portal (``/api/v1/vendor-portal``). Epic 13 ``/me``; Epic 14 owns this file: invitation acceptance,
password + mandatory TOTP sign-in, password reset, ``/csrf`` re-issue, logout and vendor self-administration
(``/users``, vendor admins only, last-admin guard). Epic 17 adds ``vendor_portal_<domain>.py`` routers
under the
same prefix. Vendor Users never reach internal routes.

Every JSON response is scanned for FR-80 forbidden fields outside production (``portal_guard_route``).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Cookie, Request, Response, status

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.core.security import MFA_COOKIE, VENDOR_COOKIE, clear_auth_cookies, set_csrf_cookie
from app.core.settings import get_settings
from app.dependencies.vendor import VendorDBSessionDep, VendorDep
from app.router.client_portal import apply_sign_in
from app.schemas.external_auth import ExternalMeOut
from app.schemas.external_auth_portal import (
    PortalAuthOut,
    PortalInviteAcceptIn,
    PortalInviteOut,
    PortalLinkVerifyIn,
    PortalMfaIn,
    PortalPasswordLoginIn,
    PortalPasswordResetConfirmIn,
    PortalPasswordResetRequestIn,
    PortalStatusOut,
    VendorPortalUserInviteIn,
    VendorPortalUserOut,
    VendorPortalUserUpdate,
)
from app.services import external_auth_service as svc
from app.services.external_auth import access, signin

router = APIRouter(
    prefix="/vendor-portal", tags=["vendor-portal"], route_class=portal_guard_route("vendor_user")
)
auth_router = APIRouter(
    prefix="/vendor-portal/auth", tags=["vendor-portal"], route_class=portal_guard_route("vendor_user")
)

MfaChallengeCookie = Annotated[str | None, Cookie(alias=MFA_COOKIE)]


@router.get("/me", response_model=ExternalMeOut)
async def me(_: VendorDep, db: VendorDBSessionDep) -> Any:
    return await svc.me(get_context(), db)


@router.get("/csrf", status_code=status.HTTP_204_NO_CONTENT)
async def reissue_csrf(_: VendorDep, request: Request, response: Response) -> None:
    """FR-82: re-issue ``tos_csrf`` for the Vendor User session (the client retries the failed call once)."""
    from app.core.security import csrf_token_from_request

    token = csrf_token_from_request(request, VENDOR_COOKIE)
    set_csrf_cookie(response, token, get_settings().external_session_absolute_hours * 3600)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(_: VendorDep, response: Response) -> None:
    await signin.logout(get_context())
    clear_auth_cookies(response, VENDOR_COOKIE)


# ---------------------------------------------------------------- self-administration (vendor admins)
@router.get("/users", response_model=list[VendorPortalUserOut])
async def list_users(_: VendorDep, db: VendorDBSessionDep) -> Any:
    return await access.portal_list_users(get_context(), db)


@router.post("/users", response_model=VendorPortalUserOut, status_code=status.HTTP_201_CREATED)
async def invite_user(body: VendorPortalUserInviteIn, _: VendorDep, db: VendorDBSessionDep) -> Any:
    return await access.portal_invite_user(get_context(), db, body)


@router.patch("/users/{vendor_user_id}", response_model=VendorPortalUserOut)
async def update_user(
    vendor_user_id: uuid.UUID, body: VendorPortalUserUpdate, _: VendorDep, db: VendorDBSessionDep
) -> Any:
    return await access.portal_update_user(get_context(), db, vendor_user_id, body)


@router.post("/users/{vendor_user_id}/deactivate", response_model=VendorPortalUserOut)
async def deactivate_user(vendor_user_id: uuid.UUID, _: VendorDep, db: VendorDBSessionDep) -> Any:
    return await access.portal_set_status(get_context(), db, vendor_user_id, active=False)


@router.post("/users/{vendor_user_id}/reactivate", response_model=VendorPortalUserOut)
async def reactivate_user(vendor_user_id: uuid.UUID, _: VendorDep, db: VendorDBSessionDep) -> Any:
    return await access.portal_set_status(get_context(), db, vendor_user_id, active=True)


# ---------------------------------------------------------------- public sign-in
@auth_router.post("/invitations/lookup", response_model=PortalInviteOut)
async def lookup_invitation(body: PortalLinkVerifyIn) -> Any:
    return await signin.lookup_vendor_invite(body.token)


@auth_router.post("/invitations/accept", response_model=PortalAuthOut)
async def accept_invitation(body: PortalInviteAcceptIn, response: Response) -> Any:
    return apply_sign_in(response, await signin.accept_vendor_invite(get_context(), body), VENDOR_COOKIE)


@auth_router.post("/login", response_model=PortalAuthOut)
async def login(body: PortalPasswordLoginIn, response: Response) -> Any:
    return apply_sign_in(response, await signin.vendor_login(get_context(), body), VENDOR_COOKIE)


@auth_router.post("/mfa/verify", response_model=PortalAuthOut)
async def mfa_verify(body: PortalMfaIn, response: Response, tos_mfa: MfaChallengeCookie = None) -> Any:
    return apply_sign_in(
        response, await signin.verify_mfa(get_context(), "vendor_user", tos_mfa, body), VENDOR_COOKIE
    )


@auth_router.post(
    "/password-reset/request", response_model=PortalStatusOut, status_code=status.HTTP_202_ACCEPTED
)
async def password_reset_request(body: PortalPasswordResetRequestIn, background: BackgroundTasks) -> Any:
    return await signin.request_vendor_reset(get_context(), body, background)


@auth_router.post("/password-reset/confirm", response_model=PortalStatusOut)
async def password_reset_confirm(body: PortalPasswordResetConfirmIn, response: Response) -> Any:
    result = await signin.confirm_vendor_reset(get_context(), body)
    clear_auth_cookies(response, VENDOR_COOKIE)
    return result
