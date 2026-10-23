"""``GET /api/public/v1/meta`` and ``GET /api/public/v1/openapi.json`` (Epic 66, spec 0.7)."""

import uuid
from typing import Any

from fastapi import Request
from pydantic import BaseModel

from app.core.public_api import API_VERSION, public_openapi, public_router
from app.core.tenancy import is_sandbox
from app.dependencies.public import PublicPrincipalDep
from app.services import identity_bridge

router = public_router("meta")


class MetaTenant(BaseModel):
    id: uuid.UUID
    slug: str | None
    sandbox: bool


class MetaPrincipal(BaseModel):
    type: str
    app_id: uuid.UUID | None
    installation_id: uuid.UUID | None
    user_id: uuid.UUID | None


class MetaOut(BaseModel):
    api_version: str
    tenant: MetaTenant
    principal: MetaPrincipal
    scopes: list[str]


@router.get("/meta", response_model=MetaOut)
async def meta(principal: PublicPrincipalDep) -> MetaOut:
    assert principal.tenant_id is not None
    from app.core.database import platform_session

    async with platform_session() as pdb:
        tenant = await identity_bridge.get_tenant(pdb, principal.tenant_id)
        slug = str(tenant.slug) if tenant is not None else None
    if principal.type == "app":
        scopes = sorted(principal.oauth_scopes)
        p = MetaPrincipal(
            type="app", app_id=principal.app_id, installation_id=principal.app_installation_id,
            user_id=principal.acting_user_id,
        )  # fmt: skip
    else:
        scopes = sorted(principal.permissions)
        p = MetaPrincipal(type=principal.type, app_id=None, installation_id=None, user_id=None)
    return MetaOut(
        api_version=API_VERSION,
        tenant=MetaTenant(id=principal.tenant_id, slug=slug, sandbox=await is_sandbox(principal.tenant_id)),
        principal=p,
        scopes=scopes,
    )


@router.get("/openapi.json")
async def openapi(request: Request, principal: PublicPrincipalDep) -> dict[str, Any]:  # ruff: ignore[unused-function-argument]
    return public_openapi(request.app)
