"""``/api/v1`` router: core endpoints + every layered router (``app.router.get_layered_routers``)."""

from typing import Any

from fastapi import APIRouter

import app.models  # noqa: F401  - registers every table before the first mapper configuration
from app.core.errors import error_response
from app.core.settings import get_settings
from app.router import base, get_layered_routers


def build_api_router() -> APIRouter:
    api = APIRouter(prefix="/api/v1")

    @api.get("/health", tags=["base"], response_model=None)
    async def health() -> Any:
        db = await base.check_database()
        expose = get_settings().app_env != "production"
        if not db.ok:
            details = {"database": db.error} if expose else None
            return error_response(503, "service_unavailable", "Database unavailable", details)
        body: dict[str, Any] = {"status": "ok", "database": "ok"}
        if expose:
            body["db_role"] = db.db_user
        return body

    for router in get_layered_routers():
        api.include_router(router)

    # Module gateway: the single backend surface the unified shell uses to render other products
    # (Croar, Feedback IQ, Portal) as native pages — /api/v1/modules/{module}/{path}.
    from app.router.modules import router as modules_router

    api.include_router(modules_router)

    # Internal service-to-service surface — /api/v1/internal/* (X-Internal-Secret; not a public / gateway route).
    from app.router.internal import router as internal_router

    api.include_router(internal_router)
    return api
