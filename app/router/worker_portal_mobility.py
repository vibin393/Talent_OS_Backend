# ruff: file-ignore[line-too-long]
"""Worker "My Work -> Opportunities" (Epic 41, ``/api/v1/portal/work/opportunities*``): the internal job board, apply
with manager approval, My internal applications, withdraw. Any Worker (a Candidate with a non-terminal Placement)
while ``worker_board_enabled``; Worker-safe payloads only (``schemas/mobility_portal``: no client, no rates /
compensation, no hiring-manager contact). The board rows and the Worker's own applications are read in the Worker's
org-scoped session (another Worker's applications are invisible -> 404); JSON responses are scanned for
worker-forbidden fields outside production."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.schemas.mobility import ApplyIn
from app.schemas.mobility_portal import WorkerApplicationOut, WorkerOpeningDetailOut, WorkerOpeningPage
from app.services.mobility import portal

router = APIRouter(prefix="/portal/work/opportunities", tags=["portal-work"], route_class=portal_guard_route("worker"))


@router.get("", response_model=WorkerOpeningPage)
async def openings(
    principal: WorkerDep,
    db: WorkerDBSessionDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    location: Annotated[str | None, Query(max_length=200)] = None,
    include_ineligible: bool = False,
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Any:
    return await portal.worker_board(principal, db, q=q, location=location, include_ineligible=include_ineligible, page=page, page_size=page_size)


@router.get("/applications", response_model=list[WorkerApplicationOut])
async def my_applications(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.worker_applications(principal, db)


@router.post("/applications/{mobility_application_id}/withdraw", response_model=WorkerApplicationOut)
async def withdraw(mobility_application_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.worker_withdraw(principal, db, mobility_application_id)


@router.get("/{demand_id}", response_model=WorkerOpeningDetailOut)
async def opening(demand_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.worker_opening(principal, db, demand_id)


@router.post("/{demand_id}/apply", response_model=WorkerApplicationOut, status_code=status.HTTP_201_CREATED)
async def apply(demand_id: uuid.UUID, body: ApplyIn, principal: WorkerDep) -> Any:
    return await portal.worker_apply(principal, demand_id, body, get_context().ip)
