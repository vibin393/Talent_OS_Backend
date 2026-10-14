"""Entitlements HTTP endpoints + enforcement dependency (Phase 1, Epic 4).

* ``GET /api/v1/entitlements/me`` — the signed-in tenant's plan + enabled modules (drives nav).
* ``GET /api/v1/entitlements/check/{module}`` — 200 if the tenant has the module, else 403.
* ``require_module(module)`` — a dependency other routers attach to gate a module at the API boundary.
"""

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Path

from app.core.errors import Forbidden, Unauthorized
from app.core.security import Principal
from app.dependencies.auth import CurrentUserDep
from app.services import entitlements

router = APIRouter(prefix="/entitlements", tags=["entitlements"])


@router.get("/me")
async def my_entitlements(principal: CurrentUserDep) -> dict[str, object]:
    if principal.tenant_id is None:
        raise Unauthorized()
    plan = await entitlements.tenant_plan(principal.tenant_id)
    modules = sorted(await entitlements.tenant_modules(principal.tenant_id))
    return {"plan": plan, "modules": modules}


@router.get("/check/{module}")
async def check_module(
    principal: CurrentUserDep, module: str = Path(min_length=1, max_length=40)
) -> dict[str, object]:
    if principal.tenant_id is None:
        raise Unauthorized()
    allowed = await entitlements.tenant_has_module(principal.tenant_id, module)
    if not allowed:
        raise Forbidden(f"module '{module}' is not in this tenant's plan", code="module_not_entitled")
    return {"module": module, "entitled": True}


def require_module(module: str) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory: reject (403) if the current tenant's plan does not include ``module``."""

    async def _dep(principal: CurrentUserDep) -> Principal:
        if principal.tenant_id is None:
            raise Unauthorized()
        if not await entitlements.tenant_has_module(principal.tenant_id, module):
            raise Forbidden(f"module '{module}' is not in this tenant's plan", code="module_not_entitled")
        return principal

    return _dep
