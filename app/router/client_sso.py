"""Client Portal SSO (Epic 50, FR-250..FR-252, AD-48).

* ``/api/v1/clients/{client_id}/sso/...`` (``client_sso:manage``): the Client's identity providers (CRUD,
  SAML metadata import, test sign-in, SP metadata), enforcement settings and break-glass users.
* ``/api/v1/client-portal/auth/sso/...`` (public, no CSRF; ``Origin`` checked on ``discover``): discovery,
  start (redirect to the IdP) and the TOTP enrolment read-back after an SSO redirect. The IdP callbacks are
  the
  Epic 14 ones (``/api/v1/auth/sso/oidc/callback``, ``/api/v1/auth/sso/saml/{idp_id}/acs``).
"""

import uuid
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.core.context import get_context
from app.core.errors import DomainError, Forbidden
from app.core.field_visibility import portal_guard_route
from app.core.permissions import Principal, require
from app.core.security import MFA_COOKIE
from app.core.settings import get_settings
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.client_sso import (
    ClientIdentityProviderIn,
    ClientIdentityProviderOut,
    ClientIdentityProviderUpdate,
    ClientSsoDiscoverIn,
    ClientSsoDiscoverOut,
    ClientSsoExemptionIn,
    ClientSsoExemptionOut,
    ClientSsoSettingsIn,
    ClientSsoSettingsOut,
    ClientTestLoginOut,
    MetadataImportIn,
)
from app.schemas.external_auth_portal import PortalAuthOut
from app.services import sso_service as svc
from app.services.external_auth import signin

router = APIRouter(prefix="/clients/{client_id}/sso", tags=["client-sso"])
portal_router = APIRouter(
    prefix="/client-portal/auth/sso", tags=["client-portal"], route_class=portal_guard_route("client_user")
)

Manage = Annotated[Principal, Depends(require("client_sso:manage"))]
DB = TenantDBSessionDep
admin = svc.client_admin


# ================================================================ identity providers
@router.get("/identity-providers", response_model=list[ClientIdentityProviderOut])
async def list_providers(client_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await admin.list_providers(get_context(), db, client_id)


@router.post(
    "/identity-providers", response_model=ClientIdentityProviderOut, status_code=status.HTTP_201_CREATED
)
async def create_provider(client_id: uuid.UUID, body: ClientIdentityProviderIn, _: Manage, db: DB) -> Any:
    return await admin.create_provider(get_context(), db, client_id, body)


@router.get("/identity-providers/{idp_id}", response_model=ClientIdentityProviderOut)
async def get_provider(client_id: uuid.UUID, idp_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await admin.get_provider(get_context(), db, client_id, idp_id)


@router.patch("/identity-providers/{idp_id}", response_model=ClientIdentityProviderOut)
async def update_provider(
    client_id: uuid.UUID, idp_id: uuid.UUID, body: ClientIdentityProviderUpdate, _: Manage, db: DB
) -> Any:
    return await admin.update_provider(get_context(), db, client_id, idp_id, body)


@router.delete("/identity-providers/{idp_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(client_id: uuid.UUID, idp_id: uuid.UUID, _: Manage, db: DB) -> Response:
    await admin.delete_provider(get_context(), db, client_id, idp_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/identity-providers/{idp_id}/metadata-import", response_model=ClientIdentityProviderOut)
async def import_metadata(
    client_id: uuid.UUID, idp_id: uuid.UUID, body: MetadataImportIn, _: Manage, db: DB
) -> Any:
    return await admin.import_metadata(get_context(), db, client_id, idp_id, body)


@router.post("/identity-providers/{idp_id}/test", response_model=ClientTestLoginOut)
async def test_login(client_id: uuid.UUID, idp_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await admin.test_login_url(get_context(), db, client_id, idp_id)


@router.get(
    "/identity-providers/{idp_id}/sp-metadata",
    response_class=Response,
    responses={200: {"content": {"application/xml": {}}}},
)
async def sp_metadata(client_id: uuid.UUID, idp_id: uuid.UUID, _: Manage, db: DB) -> Response:
    xml = await admin.sp_metadata(get_context(), db, client_id, idp_id)
    return Response(content=xml, media_type="application/xml")


# ================================================================ enforcement and break-glass
@router.get("/settings", response_model=ClientSsoSettingsOut)
async def get_settings_(client_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await admin.get_settings(get_context(), db, client_id)


@router.put("/settings", response_model=ClientSsoSettingsOut)
async def put_settings(client_id: uuid.UUID, body: ClientSsoSettingsIn, _: Manage, db: DB) -> Any:
    return await admin.put_settings(get_context(), db, client_id, body)


@router.get("/exemptions", response_model=list[ClientSsoExemptionOut])
async def list_exemptions(client_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await admin.list_exemptions(get_context(), db, client_id)


@router.post("/exemptions", response_model=ClientSsoExemptionOut, status_code=status.HTTP_201_CREATED)
async def add_exemption(client_id: uuid.UUID, body: ClientSsoExemptionIn, _: Manage, db: DB) -> Any:
    return await admin.add_exemption(get_context(), db, client_id, body)


@router.delete("/exemptions/{client_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_exemption(client_id: uuid.UUID, client_user_id: uuid.UUID, _: Manage, db: DB) -> Response:
    await admin.remove_exemption(get_context(), db, client_id, client_user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ================================================================ Client Portal (public)
def _frontend() -> str:
    return get_settings().frontend_url.rstrip("/")


@portal_router.post("/discover", response_model=ClientSsoDiscoverOut)
async def discover(body: ClientSsoDiscoverIn, request: Request) -> Any:
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin and origin != _frontend():
        raise Forbidden("Cross-origin request refused", code="origin_not_allowed")
    return await svc.client_discover(get_context(), body)


@portal_router.get("/{idp_id}/start", response_class=Response, include_in_schema=False)
async def start(
    idp_id: uuid.UUID,
    request: Request,
    tenant: Annotated[str, Query(min_length=1, max_length=63)],
    next: Annotated[str | None, Query(max_length=500)] = None,
    test: bool = False,
) -> Response:
    try:
        url = await svc.start(get_context(), idp_id, tenant, next, test=test, audience="client")
    except DomainError as exc:
        if "application/json" in (request.headers.get("accept") or ""):
            raise
        return RedirectResponse(
            f"{_frontend()}/client/sign-in?sso_error={quote(exc.code)}", status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@portal_router.post("/mfa-enrollment", response_model=PortalAuthOut)
async def mfa_enrollment(tos_mfa: Annotated[str | None, Cookie(alias=MFA_COOKIE)] = None) -> Any:
    """After an SSO redirect to ``/client/sign-in?mfa=enroll``: the TOTP enrolment (secret shown once)."""
    return await signin.client_sso_mfa_enrollment(tos_mfa)
