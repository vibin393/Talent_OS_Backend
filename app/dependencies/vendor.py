"""Vendor User dependencies (Epic 13, AD-26). Vendor Portal routers (``/api/v1/vendor-portal``) use these.

* ``require_vendor_user`` / ``VendorDep``: the ``tos_vendor`` session (vendor must be active / onboarding).
* ``get_vendor_db`` / ``VendorDBSessionDep``: a tenant session with ``OrgScope.of("vendor", vendor_id)``.
* ``require_portal_permission(key)``.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import OrgScope, tenant_session
from app.core.errors import Forbidden, Unauthorized
from app.core.permissions import Principal


async def require_vendor_user(request: Request) -> Principal:
    from app.services.external_auth import sessions

    return await sessions.resolve(request, "vendor_user")


VendorDep = Annotated[Principal, Depends(require_vendor_user)]


async def get_vendor_db(principal: VendorDep) -> AsyncIterator[AsyncSession]:
    if principal.tenant_id is None or principal.org_id is None:
        raise Unauthorized()
    async with tenant_session(principal.tenant_id, org=OrgScope.of("vendor", principal.org_id)) as session:
        yield session


VendorDBSessionDep = Annotated[AsyncSession, Depends(get_vendor_db)]


def require_portal_permission(key: str) -> Callable[..., Awaitable[Principal]]:
    async def dependency(principal: VendorDep) -> Principal:  # NOSONAR
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

    dependency.__name__ = f"require_vendor_{key.replace(':', '_')}"
    return dependency
