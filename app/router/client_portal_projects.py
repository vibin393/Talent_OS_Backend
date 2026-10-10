# ruff: noqa: E501
"""Client Portal projects (Epic 45, FR-231): ``/api/v1/client-portal/projects`` and deliverable decisions.

Client session (org RLS ``client``) + the ``projects:view`` portal permission to read, ``deliverables:accept`` to
accept or reject (★ actor = the Client User). Another Client's project / deliverable is 404; restricted Client
Users see only projects of their scoped Placements. Responses use ``schemas/projects_portal.py`` only (no cost,
rate, budget, burn, vendor or internal notes); every JSON response is scanned outside production.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.core.permissions import Principal
from app.dependencies.client import ClientDBSessionDep, require_portal_permission
from app.schemas.projects_portal import (
    DeliverablePortalOut,
    DeliverableRejectPortalIn,
    ProjectPortalDetail,
    ProjectPortalOut,
)
from app.services.projects import portal

router = APIRouter(
    prefix="/client-portal", tags=["client-portal"], route_class=portal_guard_route("client_user")
)
Viewer = Annotated[Principal, Depends(require_portal_permission("projects:view"))]
Acceptor = Annotated[Principal, Depends(require_portal_permission("deliverables:accept"))]


@router.get("/projects", response_model=list[ProjectPortalOut])
async def list_projects(_: Viewer, db: ClientDBSessionDep) -> Any:
    return await portal.client_list(get_context(), db)


@router.get("/projects/{project_id}", response_model=ProjectPortalDetail)
async def get_project(project_id: uuid.UUID, _: Viewer, db: ClientDBSessionDep) -> Any:
    return await portal.client_detail(get_context(), db, project_id)


@router.post("/deliverables/{deliverable_id}/accept", response_model=DeliverablePortalOut)
async def accept_deliverable(deliverable_id: uuid.UUID, _: Acceptor, db: ClientDBSessionDep) -> Any:
    return await portal.client_decide(get_context(), db, deliverable_id, "accept")


@router.post("/deliverables/{deliverable_id}/reject", response_model=DeliverablePortalOut)
async def reject_deliverable(
    deliverable_id: uuid.UUID, body: DeliverableRejectPortalIn, _: Acceptor, db: ClientDBSessionDep
) -> Any:
    return await portal.client_decide(get_context(), db, deliverable_id, "reject", body.reason)
