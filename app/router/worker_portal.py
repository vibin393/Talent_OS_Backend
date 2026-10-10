"""Worker "My Work" (``/api/v1/portal/work``; AD-27: the Candidate principal with the worker org scope).
Epic 13 ``/me`` only; domain epics add ``worker_portal_<domain>.py`` routers under the same prefix."""

from typing import Any

from fastapi import APIRouter

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.schemas.external_auth import ExternalMeOut
from app.services import external_auth_service as svc

# Epic 14 (FR-80): JSON responses are scanned for worker-forbidden fields outside production.
router = APIRouter(prefix="/portal/work", tags=["portal-work"], route_class=portal_guard_route("worker"))


@router.get("/me", response_model=ExternalMeOut)
async def me(_: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await svc.me(get_context(), db)
