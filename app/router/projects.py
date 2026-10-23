# ruff: file-ignore[line-too-long]
"""Projects & SOW delivery (Epic 45, FR-227..FR-231): ``/api/v1/projects`` and friends.

Permissions: ``project:read`` / ``project:write`` (data-scoped on the project's owner / business unit; 404 outside
the scope) and ``deliverable:record`` (recording an offline Client acceptance / rejection). External (client /
vendor) principals never reach these routes (AD-26). Each endpoint calls one service function.
"""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.projects import (
    AssignmentEndIn,
    AssignmentIn,
    AssignmentOut,
    BudgetOut,
    BudgetsIn,
    BurnOut,
    DeliverableIn,
    DeliverableOut,
    DeliverableRejectIn,
    DeliverableUpdate,
    DemandFromRoleIn,
    Project360Out,
    ProjectCreate,
    ProjectListItem,
    ProjectOut,
    ProjectRoleIn,
    ProjectRoleOut,
    ProjectRoleUpdate,
    ProjectTransitionIn,
    ProjectUpdate,
    TimelineOut,
)
from app.services.projects import assignments, budgets, burn, deliverables, overview, projects, roles

router = APIRouter(tags=["projects"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("project:read"))]
Write = Annotated[Principal, Depends(require("project:write"))]
Record = Annotated[Principal, Depends(require("deliverable:record"))]


# ================================================================ projects
@router.get("/projects", response_model=list[ProjectListItem])
async def list_projects(
    _: Read,
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    client_id: uuid.UUID | None = None,
    contract_id: uuid.UUID | None = None,
    q: str | None = None,
    include_archived: bool = False,
) -> Any:
    return await projects.list_projects(
        get_context(),
        db,
        status=status_,
        client_id=client_id,
        contract_id=contract_id,
        q=q,
        include_archived=include_archived,
    )


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, _: Write, db: DB) -> Any:
    return await projects.create_project(get_context(), db, body)


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await projects.get_project_out(get_context(), db, project_id)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def update_project(project_id: uuid.UUID, body: ProjectUpdate, _: Write, db: DB) -> Any:
    return await projects.update_project(get_context(), db, project_id, body)


@router.post("/projects/{project_id}/transition", response_model=ProjectOut)
async def transition_project(project_id: uuid.UUID, body: ProjectTransitionIn, _: Write, db: DB) -> Any:
    return await projects.transition(get_context(), db, project_id, body.to)


@router.get("/projects/{project_id}/360", response_model=Project360Out)
async def project_360(project_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await overview.project_360(get_context(), db, project_id)


@router.get("/projects/{project_id}/resource-timeline", response_model=TimelineOut)
async def resource_timeline(
    project_id: uuid.UUID,
    _: Read,
    db: DB,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
) -> Any:
    return await overview.get_timeline(get_context(), db, project_id, from_, to)


# ================================================================ roles
@router.get("/projects/{project_id}/roles", response_model=list[ProjectRoleOut])
async def list_roles(project_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await roles.list_roles(get_context(), db, project_id)


@router.post(
    "/projects/{project_id}/roles", response_model=ProjectRoleOut, status_code=status.HTTP_201_CREATED
)
async def create_role(project_id: uuid.UUID, body: ProjectRoleIn, _: Write, db: DB) -> Any:
    return await roles.create_role(get_context(), db, project_id, body)


@router.patch("/project-roles/{role_id}", response_model=ProjectRoleOut)
async def update_role(role_id: uuid.UUID, body: ProjectRoleUpdate, _: Write, db: DB) -> Any:
    return await roles.update_role(get_context(), db, role_id, body)


@router.delete("/project-roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(role_id: uuid.UUID, _: Write, db: DB) -> Response:
    await roles.delete_role(get_context(), db, role_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/projects/{project_id}/roles/{role_id}/demand", status_code=status.HTTP_201_CREATED)
async def demand_from_role(
    project_id: uuid.UUID, role_id: uuid.UUID, body: DemandFromRoleIn, _: Write, db: DB
) -> Any:
    return await roles.create_demand_for(get_context(), db, project_id, role_id, body.positions)


# ================================================================ assignments
@router.get("/projects/{project_id}/assignments", response_model=list[AssignmentOut])
async def list_project_assignments(project_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await assignments.list_for_project(get_context(), db, project_id)


@router.get("/project-roles/{role_id}/assignments", response_model=list[AssignmentOut])
async def list_role_assignments(role_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await assignments.list_for_role(get_context(), db, role_id)


@router.post(
    "/project-roles/{role_id}/assignments", response_model=AssignmentOut, status_code=status.HTTP_201_CREATED
)
async def start_assignment(role_id: uuid.UUID, body: AssignmentIn, _: Write, db: DB) -> Any:
    return await assignments.start(get_context(), db, role_id, body)


@router.post("/project-assignments/{assignment_id}/end", response_model=AssignmentOut)
async def end_assignment(assignment_id: uuid.UUID, body: AssignmentEndIn, _: Write, db: DB) -> Any:
    return await assignments.end(get_context(), db, assignment_id, body.end_date, body.reason)


# ================================================================ deliverables
@router.get("/projects/{project_id}/deliverables", response_model=list[DeliverableOut])
async def list_deliverables(project_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await deliverables.list_for_project(get_context(), db, project_id)


@router.post(
    "/projects/{project_id}/deliverables", response_model=DeliverableOut, status_code=status.HTTP_201_CREATED
)
async def create_deliverable(project_id: uuid.UUID, body: DeliverableIn, _: Write, db: DB) -> Any:
    return await deliverables.create(get_context(), db, project_id, body)


@router.get("/deliverables/{deliverable_id}", response_model=DeliverableOut)
async def get_deliverable(deliverable_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await deliverables.get_deliverable(get_context(), db, deliverable_id)


@router.patch("/deliverables/{deliverable_id}", response_model=DeliverableOut)
async def update_deliverable(deliverable_id: uuid.UUID, body: DeliverableUpdate, _: Write, db: DB) -> Any:
    return await deliverables.update(get_context(), db, deliverable_id, body)


@router.post("/deliverables/{deliverable_id}/start", response_model=DeliverableOut)
async def start_deliverable(deliverable_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await deliverables.move(get_context(), db, deliverable_id, "in_progress")


@router.post("/deliverables/{deliverable_id}/submit", response_model=DeliverableOut)
async def submit_deliverable(deliverable_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await deliverables.move(get_context(), db, deliverable_id, "submitted")


@router.post("/deliverables/{deliverable_id}/cancel", response_model=DeliverableOut)
async def cancel_deliverable(deliverable_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await deliverables.move(get_context(), db, deliverable_id, "cancelled")


@router.post("/deliverables/{deliverable_id}/record-acceptance", response_model=DeliverableOut)
async def record_acceptance(
    deliverable_id: uuid.UUID,
    _: Record,
    db: DB,
    file: Annotated[UploadFile | None, File()] = None,
    accepted_by_contact_id: Annotated[uuid.UUID | None, Form()] = None,
) -> Any:
    data = await file.read() if file is not None else None
    return await deliverables.record_acceptance(
        get_context(),
        db,
        deliverable_id,
        data=data,
        filename=file.filename if file is not None else None,
        accepted_by_contact_id=accepted_by_contact_id,
    )


@router.post("/deliverables/{deliverable_id}/record-rejection", response_model=DeliverableOut)
async def record_rejection(deliverable_id: uuid.UUID, body: DeliverableRejectIn, _: Record, db: DB) -> Any:
    return await deliverables.record_rejection(get_context(), db, deliverable_id, body.reason)


# ================================================================ budgets / burn
@router.get("/projects/{project_id}/budgets", response_model=list[BudgetOut])
async def list_budgets(project_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await budgets.list_budgets(get_context(), db, project_id)


@router.put("/projects/{project_id}/budgets", response_model=list[BudgetOut])
async def set_budgets(project_id: uuid.UUID, body: BudgetsIn, _: Write, db: DB) -> Any:
    return await budgets.set_budgets(get_context(), db, project_id, body)


@router.get("/projects/{project_id}/burn", response_model=list[BurnOut])
async def list_burn(project_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await burn.list_burn(get_context(), db, project_id)


@router.post("/projects/{project_id}/burn/rebuild", response_model=list[BurnOut])
async def rebuild_burn(project_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await burn.rebuild(get_context(), db, project_id)
