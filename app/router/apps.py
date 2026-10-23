# ruff: file-ignore[line-too-long]
"""Private Apps and Connected apps (Epic 67, spec A6; ``/api/v1``). User sessions only: API keys and app tokens get
403 ``human_session_required`` (app tokens are refused earlier by the resolver: ``app_token_not_allowed``)."""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.context import get_context
from app.core.errors import Forbidden, ValidationFailed
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.apps import (
    AppDetailOut,
    AppSecretOut,
    ConnectedAppOut,
    RetireAppIn,
    RevokeSecretIn,
    ScopeCatalogueOut,
)
from app.schemas.metering import UsageOut, UsageQuery
from app.schemas.oauth import AppCreate, AppCreated, AppOut, AppUpdate, Publisher, SecretCreated
from app.services import api_keys_service, metering_service, oauth_service

router = APIRouter(tags=["apps"])
DB = TenantDBSessionDep
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _human_session(principal: CurrentUserDep) -> Principal:
    if principal.type != "user":
        raise Forbidden("Sign in to Talent OS to manage apps", code="human_session_required")
    return principal


Human = Annotated[Principal, Depends(_human_session)]
Develop = Annotated[Principal, Depends(require("app:develop"))]
Read = Annotated[Principal, Depends(require_any("app:develop", "integration:view"))]
KeyManage = Annotated[Principal, Depends(require("api_key:manage"))]


@router.get("/apps", response_model=list[AppOut])
async def list_apps(_h: Human, _: Read, db: DB) -> list[AppOut]:
    return await oauth_service.list_private_apps(get_context(), db)


@router.get("/apps/scopes", response_model=list[ScopeCatalogueOut])
async def list_scopes(_h: Human, _: Develop) -> list[dict[str, object]]:
    return oauth_service.scope_catalogue_out()


@router.post("/apps", response_model=AppCreated, status_code=status.HTTP_201_CREATED)
async def create_app(body: AppCreate, response: Response, principal: Human, _: Develop, db: DB) -> AppCreated:
    response.headers.update(NO_STORE)
    publisher = Publisher(kind="tenant", tenant_id=principal.tenant_id, created_by_id=principal.id)
    return await oauth_service.create_app(get_context(), db, body, publisher=publisher)


@router.get("/apps/{app_id}", response_model=AppDetailOut)
async def get_app(app_id: uuid.UUID, _h: Human, _: Read, db: DB) -> AppDetailOut:
    ctx = get_context()
    out = await oauth_service.get_private_app(ctx, db, app_id)
    secrets = [AppSecretOut.model_validate(s) for s in await oauth_service.list_secrets(ctx, app_id)]
    inst = await oauth_service.installation_for_app(ctx, db, app_id)
    return AppDetailOut(**out.model_dump(), secrets=secrets, installation_id=inst)


@router.patch("/apps/{app_id}", response_model=AppOut)
async def update_app(app_id: uuid.UUID, body: AppUpdate, _h: Human, _: Develop, db: DB) -> AppOut:
    return await oauth_service.update_app(get_context(), db, app_id, body)


@router.post("/apps/{app_id}/secrets/rotate", response_model=SecretCreated, status_code=status.HTTP_201_CREATED)
async def rotate_secret(app_id: uuid.UUID, response: Response, _h: Human, _: Develop, db: DB) -> SecretCreated:
    response.headers.update(NO_STORE)
    return await oauth_service.rotate_secret(get_context(), db, app_id)


@router.post("/apps/{app_id}/secrets/{secret_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_secret(app_id: uuid.UUID, secret_id: uuid.UUID, body: RevokeSecretIn, _h: Human, _: Develop, db: DB) -> Response:
    await oauth_service.revoke_secret(get_context(), db, app_id, secret_id, confirm_last=body.confirm_last)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/apps/{app_id}/retire", response_model=AppOut)
async def retire_app(app_id: uuid.UUID, body: RetireAppIn, _h: Human, _: Develop, db: DB) -> AppOut:
    return await oauth_service.retire_app(get_context(), db, app_id, reason=body.reason)


@router.get("/apps/{app_id}/usage", response_model=UsageOut)
async def app_usage(
    app_id: uuid.UUID,
    _h: Human,
    _: Annotated[Principal, Depends(require_any("app:develop", "usage:read"))],
    db: DB,
    date_from: Annotated[dt.date, Query(alias="from")],
    date_to: Annotated[dt.date, Query(alias="to")],
) -> UsageOut:
    ctx = get_context()
    inst = await oauth_service.installation_for_app(ctx, db, app_id)
    try:
        q = UsageQuery(date_from=date_from, date_to=date_to, installation_id=inst or uuid.UUID(int=0))
    except ValueError as exc:
        raise ValidationFailed("Invalid usage query", details={"error": str(exc)[:300]}) from exc
    return await metering_service.usage(ctx, db, q)


@router.get("/me/connected-apps", response_model=list[ConnectedAppOut])
async def connected_apps(_h: Human, db: DB) -> list[dict[str, object]]:
    return await oauth_service.list_connected_apps(get_context(), db)


@router.delete("/me/connected-apps/{authorization_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_connected_app(authorization_id: uuid.UUID, _h: Human, db: DB) -> Response:
    await oauth_service.revoke_my_authorization(get_context(), db, authorization_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api-keys/public-scopes", response_model=list[str])
async def public_scopes(_: KeyManage, scopes: Annotated[list[str] | None, Query()] = None) -> list[str]:
    flat = [s for raw in scopes or [] for s in raw.split(",") if s]
    return api_keys_service.public_scopes_for(flat)
