# ruff: file-ignore[line-too-long]
"""Public API dependencies (Epic 66, AD-77, spec 0.6 / 0.7).

``public_principal`` accepts ``Authorization: Bearer``:

* ``tos_at_`` -> ``oauth_service.authenticate_bearer(resource="api")`` (an ``mcp`` token here -> 401 wrong resource);
* a ``tos_`` API key -> ``api_keys_service.authenticate`` (its own ``rate_limit_per_min``).

Then the ecosystem gate (403 ``ecosystem_suspended`` while the tenant / platform switch is on), the Rate Tier
enforcement and the ``api_request_log`` after-response hook (``metering_service``). Cookies are never accepted here.
``PublicDBSessionDep`` is the tenant session of the authenticated principal.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.errors import Unauthorized
from app.core.permissions import Principal
from app.core.public_api import set_public_principal_resolver
from app.dependencies.auth import PRINCIPAL_STATE_ATTR_PUBLIC, _bearer, bearer_key, bearer_oauth
from app.services import api_keys_service, ecosystem_service, oauth_service


async def _meter(request: Request, principal: Principal) -> None:
    from app.services import metering_service

    await metering_service.enforce(principal, _route_group(request))
    metering_service.register_request_log(request, principal)


def _route_group(request: Request) -> str:
    path = request.url.path.removeprefix("/api/public/v1/").split("/", 1)[0]
    return path[:60]


async def public_principal(request: Request) -> Principal:
    cached = getattr(request.state, PRINCIPAL_STATE_ATTR_PUBLIC, None)
    if isinstance(cached, Principal):
        return cached
    token = bearer_oauth(request)
    if token is not None:
        principal = await oauth_service.authenticate_bearer(request, token, resource="api")
    else:
        key = bearer_key(request)
        if key is None:
            raw = _bearer(request)
            raise Unauthorized(
                "A bearer token is required" if raw is None else "Unsupported token", code="invalid_token"
            )
        principal = await api_keys_service.authenticate(request, key)
        assert principal.tenant_id is not None
        state = await ecosystem_service.is_suspended(principal.tenant_id)
        if state.suspended:
            raise oauth_service.ecosystem_suspended()
    setattr(request.state, PRINCIPAL_STATE_ATTR_PUBLIC, principal)
    await _meter(request, principal)
    return principal


set_public_principal_resolver(public_principal)

PublicPrincipalDep = Annotated[Principal, Depends(public_principal)]


async def get_public_db(principal: PublicPrincipalDep) -> AsyncIterator[AsyncSession]:
    if principal.tenant_id is None:  # defensive
        raise Unauthorized()
    async with tenant_session(principal.tenant_id) as session:
        yield session


PublicDBSessionDep = Annotated[AsyncSession, Depends(get_public_db)]

__all__ = ["PublicDBSessionDep", "PublicPrincipalDep", "get_public_db", "public_principal"]
