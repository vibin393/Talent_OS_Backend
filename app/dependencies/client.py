"""Client User dependencies (Epic 13, AD-26). Client Portal routers (``/api/v1/client-portal``) use these, never
the internal ``require()``.

* ``require_client_user`` / ``ClientDep``: the ``tos_client`` session (CSRF on mutations).
* ``get_client_db`` / ``ClientDBSessionDep``: a tenant session with ``OrgScope.of("client", client_id)``.
* ``require_portal_permission(key)``: 403 ``portal_permission_required`` unless the Client User holds ``key``.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import OrgScope, tenant_session
from app.core.errors import Forbidden, Unauthorized
from app.core.permissions import Principal


async def require_client_user(request: Request) -> Principal:
    from app.services.external_auth import sessions

    return await sessions.resolve(request, "client_user")


ClientDep = Annotated[Principal, Depends(require_client_user)]


async def get_client_db(principal: ClientDep) -> AsyncIterator[AsyncSession]:
    if principal.tenant_id is None or principal.org_id is None:
        raise Unauthorized()
    async with tenant_session(principal.tenant_id, org=OrgScope.of("client", principal.org_id)) as session:
        yield session


ClientDBSessionDep = Annotated[AsyncSession, Depends(get_client_db)]


def require_portal_permission(key: str) -> Callable[..., Awaitable[Principal]]:
    async def dependency(principal: ClientDep) -> Principal:  # NOSONAR
        if key not in principal.portal_permissions:
            raise Forbidden(
                "You do not have access to this area",
                code="portal_permission_required",
                details={"permission": key},
                action=key,
                required_permission=key,
                ask="account_manager",  # FR-82: portal principals ask their Account Manager
            )
        return principal

    dependency.__name__ = f"require_client_{key.replace(':', '_')}"
    return dependency
