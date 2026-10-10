"""Single sign-on (Epic 14, FR-77, AD-30).

* ``/api/v1/sso/...`` (``sso:manage``): identity providers, group -> Role mapping, test sign-in, SP metadata,
  break-glass Tenant Admins.
* ``/api/v1/auth/sso/...`` (public, no CSRF; ``Origin`` checked on ``discover``): discover, start, the OIDC
  callback and the SAML ACS. Browser callbacks redirect: success -> ``next`` (same origin) with the session
  cookies; MFA -> ``/login?mfa=1``; failure -> ``/login?sso_error=<code>`` (a JSON ``Accept`` gets the error
  envelope instead).
* Epic 50 (AD-48): the same callbacks serve client IdPs. A client result sets ``tos_client`` (+ CSRF) and
  redirects into ``/client``; its MFA step goes to ``/client/sign-in?mfa=1`` (``mfa=enroll`` when TOTP enrolment
  is due) and failures to ``/client/sign-in?sso_error=<code>``. No internal session cookie is set on that path.
"""

import uuid
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.context import get_context
from app.core.errors import DomainError, Forbidden, error_response
from app.core.permissions import Principal, require
from app.core.security import (
    CLIENT_COOKIE,
    MFA_COOKIE,
    USER_COOKIE,
    clear_cookie,
    set_csrf_cookie,
    set_session_cookie,
)
from app.core.settings import get_settings
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.sso import (
    BreakGlassAdminOut,
    BreakGlassIn,
    GroupRoleIn,
    IdentityProviderIn,
    IdentityProviderOut,
    IdentityProviderUpdate,
    SsoDiscoverIn,
    SsoDiscoverOut,
    TestLoginOut,
)
from app.services import sso_service as svc

router = APIRouter(prefix="/sso", tags=["sso"])
auth_router = APIRouter(prefix="/auth/sso", tags=["sso"])

Manage = Annotated[Principal, Depends(require("sso:manage"))]
DB = TenantDBSessionDep


# ================================================================ administration
@router.get("/identity-providers", response_model=list[IdentityProviderOut])
async def list_providers(_: Manage, db: DB) -> Any:
    return await svc.list_providers(get_context(), db)


@router.post("/identity-providers", response_model=IdentityProviderOut, status_code=status.HTTP_201_CREATED)
async def create_provider(body: IdentityProviderIn, _: Manage, db: DB) -> Any:
    return await svc.create_provider(get_context(), db, body)


@router.get("/identity-providers/{idp_id}", response_model=IdentityProviderOut)
async def get_provider(idp_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await svc.get_provider(get_context(), db, idp_id)


@router.patch("/identity-providers/{idp_id}", response_model=IdentityProviderOut)
async def update_provider(idp_id: uuid.UUID, body: IdentityProviderUpdate, _: Manage, db: DB) -> Any:
    return await svc.update_provider(get_context(), db, idp_id, body)


@router.delete("/identity-providers/{idp_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(idp_id: uuid.UUID, _: Manage, db: DB) -> Response:
    await svc.delete_provider(get_context(), db, idp_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/identity-providers/{idp_id}/group-roles", response_model=IdentityProviderOut)
async def set_group_roles(idp_id: uuid.UUID, body: list[GroupRoleIn], _: Manage, db: DB) -> Any:
    return await svc.set_group_roles(get_context(), db, idp_id, body)


@router.post("/identity-providers/{idp_id}/test", response_model=TestLoginOut)
async def test_login(idp_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await svc.test_login_url(get_context(), db, idp_id)


@router.get(
    "/saml/{idp_id}/metadata", response_class=Response, responses={200: {"content": {"application/xml": {}}}}
)
async def sp_metadata(idp_id: uuid.UUID, _: Manage, db: DB) -> Response:
    return Response(content=await svc.sp_metadata(get_context(), db, idp_id), media_type="application/xml")


@router.get("/break-glass-admins", response_model=list[BreakGlassAdminOut])
async def list_break_glass(_: Manage, db: DB) -> Any:
    return await svc.list_break_glass(get_context(), db)


@router.put("/break-glass-admins/{user_id}", response_model=BreakGlassAdminOut)
async def set_break_glass(user_id: uuid.UUID, body: BreakGlassIn, _: Manage, db: DB) -> Any:
    return await svc.set_break_glass(get_context(), db, user_id, body.break_glass)


# ================================================================ public sign-in
def _frontend() -> str:
    return get_settings().frontend_url.rstrip("/")


def _check_origin(request: Request) -> None:
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin and origin != _frontend():
        raise Forbidden("Cross-origin request refused", code="origin_not_allowed")


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


def _failure(request: Request, exc: DomainError) -> Response:
    if _wants_json(request):
        return error_response(exc.status_code, exc.code, exc.message, exc.details)
    page = "/client/sign-in" if svc.failure_audience(exc) == "client" else "/login"
    return RedirectResponse(f"{_frontend()}{page}?sso_error={quote(exc.code)}", status_code=status.HTTP_303_SEE_OTHER)


def _client_success(result: svc.LoginResult) -> Response:
    """Epic 50: a client IdP login -> ``tos_client`` session, or the external TOTP step."""
    signed = result.client
    if signed.session_token is not None and signed.csrf_token is not None:
        response = RedirectResponse(f"{_frontend()}{result.next_path}", status_code=status.HTTP_303_SEE_OTHER)
        set_session_cookie(response, CLIENT_COOKIE, signed.session_token, signed.max_age)
        set_csrf_cookie(response, signed.csrf_token, signed.max_age)
        clear_cookie(response, MFA_COOKIE)
        return response
    step = "enroll" if signed.auth.status == "mfa_enrollment_required" else "1"
    response = RedirectResponse(
        f"{_frontend()}/client/sign-in?mfa={step}&next={quote(result.next_path)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    if signed.mfa_token is not None:
        set_session_cookie(response, MFA_COOKIE, signed.mfa_token, signed.mfa_max_age)
    return response


def _success(result: svc.LoginResult) -> Response:
    if result.test is not None:
        return JSONResponse(result.test.model_dump(mode="json"))
    if result.client is not None:
        return _client_success(result)
    if result.mfa is not None:
        response = RedirectResponse(
            f"{_frontend()}/login?mfa=1&next={quote(result.next_path)}", status_code=status.HTTP_303_SEE_OTHER
        )
        set_session_cookie(response, MFA_COOKIE, result.mfa.token, result.mfa.max_age)
        return response
    assert result.auth is not None
    response = RedirectResponse(f"{_frontend()}{result.next_path}", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, USER_COOKIE, result.auth.session_token, result.auth.max_age)
    set_csrf_cookie(response, result.auth.csrf_token, result.auth.max_age)
    return response


@auth_router.post("/discover", response_model=SsoDiscoverOut)
async def discover(body: SsoDiscoverIn, request: Request) -> Any:
    _check_origin(request)
    return await svc.discover(get_context(), body)


@auth_router.get("/oidc/callback", response_class=Response, include_in_schema=False)
async def oidc_callback(
    request: Request,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> Response:
    try:
        if error:
            from app.services.sso.oidc import invalid

            raise invalid("idp_error")
        return _success(await svc.complete_oidc(get_context(), code, state))
    except DomainError as exc:
        return _failure(request, exc)


@auth_router.post("/saml/{idp_id}/acs", response_class=Response, include_in_schema=False)
async def saml_acs(
    idp_id: uuid.UUID,
    request: Request,
    saml_response: Annotated[str | None, Form(alias="SAMLResponse", max_length=200_000)] = None,
    relay_state: Annotated[str | None, Form(alias="RelayState", max_length=512)] = None,
) -> Response:
    try:
        return _success(await svc.complete_saml(get_context(), idp_id, saml_response, relay_state))
    except DomainError as exc:
        return _failure(request, exc)


@auth_router.get("/{idp_id}/start", response_class=Response, include_in_schema=False)
async def start(
    idp_id: uuid.UUID,
    request: Request,
    tenant: Annotated[str, Query(min_length=1, max_length=63)],
    next: Annotated[str | None, Query(max_length=500)] = None,
    test: bool = False,
) -> Response:
    try:
        url = await svc.start(get_context(), idp_id, tenant, next, test=test)
    except DomainError as exc:
        return _failure(request, exc)
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)
