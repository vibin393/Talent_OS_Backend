# ruff: file-ignore[line-too-long]
"""Workforce planning (Epic 44, FR-222..FR-225): ``/api/v1/workforce/*``.

Permissions: ``workforce_plan:read`` / ``write`` (data-scoped on the plan's owner / BU; 404 outside the scope);
slot conversion also needs ``demand:create``; ``/workforce/settings`` writes need ``workforce_plan:approve``.
Approval decisions go through the P2 approvals API (subject ``workforce_plan``, human only ★). External (client /
vendor) principals never reach these routes (AD-26). Each endpoint calls one service function.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.workforce import (
    ActualsOut,
    ConvertIn,
    ConvertOut,
    DiffOut,
    GroupBy,
    LineImportOut,
    LineIn,
    LineOut,
    LineUpdate,
    PlanCopyIn,
    PlanCreate,
    PlanOut,
    PlanUpdate,
    RevisionOut,
    RevisionStartIn,
    SlotCancelIn,
    SlotOut,
    WorkforceSettingsIn,
    WorkforceSettingsOut,
)
from app.services import workforce_service
from app.services.workforce import actuals, approval, conversion, imports, lines, plans, revisions, slots

router = APIRouter(prefix="/workforce", tags=["workforce"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("workforce_plan:read"))]
Write = Annotated[Principal, Depends(require("workforce_plan:write"))]
Approve = Annotated[Principal, Depends(require("workforce_plan:approve"))]
DemandCreate = Annotated[Principal, Depends(require("demand:create"))]


# ================================================================ settings / template
@router.get("/settings", response_model=WorkforceSettingsOut)
async def get_settings(_: Read, db: DB) -> Any:
    return await workforce_service.get_settings(get_context(), db)


@router.patch("/settings", response_model=WorkforceSettingsOut)
async def update_settings(body: WorkforceSettingsIn, _: Approve, db: DB) -> Any:
    return await workforce_service.update_settings(get_context(), db, body)


@router.get("/plans/import-template")
async def import_template(_: Write) -> Response:
    return Response(
        content=imports.TEMPLATE,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="workforce-plan-lines.csv"'},
    )


# ================================================================ plans
@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    _: Read,
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    client_id: uuid.UUID | None = None,
    origin: str | None = None,
    q: str | None = None,
) -> Any:
    return await plans.list_plans(get_context(), db, status=status_, client_id=client_id, origin=origin, q=q)


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(body: PlanCreate, _: Write, db: DB) -> Any:
    return await plans.create_plan(get_context(), db, body)


@router.get("/plans/{plan_id}", response_model=PlanOut)
async def get_plan(plan_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await plans.get_plan_out(get_context(), db, plan_id)


@router.patch("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(plan_id: uuid.UUID, body: PlanUpdate, _: Write, db: DB) -> Any:
    return await plans.update_plan(get_context(), db, plan_id, body)


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(plan_id: uuid.UUID, _: Write, db: DB) -> Response:
    await plans.delete_plan(get_context(), db, plan_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/plans/{plan_id}/copy", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def copy_plan(plan_id: uuid.UUID, body: PlanCopyIn, _: Write, db: DB) -> Any:
    return await plans.copy_plan(get_context(), db, plan_id, body)


# ---------------------------------------------------------------- transitions
@router.post("/plans/{plan_id}/budget", response_model=PlanOut)
async def budget_plan(plan_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await plans.budget(get_context(), db, plan_id)


@router.post("/plans/{plan_id}/unbudget", response_model=PlanOut)
async def unbudget_plan(plan_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await plans.unbudget(get_context(), db, plan_id)


@router.post("/plans/{plan_id}/reopen", response_model=PlanOut)
async def reopen_plan(plan_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await plans.reopen(get_context(), db, plan_id)


@router.post("/plans/{plan_id}/submit", response_model=PlanOut)
async def submit_plan(plan_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await approval.submit(get_context(), db, plan_id)


@router.post("/plans/{plan_id}/start-revision", response_model=PlanOut)
async def start_revision(plan_id: uuid.UUID, _: Write, db: DB, body: RevisionStartIn | None = None) -> Any:
    return await revisions.start_revision(get_context(), db, plan_id, body.reason if body else None)


@router.post("/plans/{plan_id}/discard-revision", response_model=PlanOut)
async def discard_revision(plan_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await revisions.discard_revision(get_context(), db, plan_id)


@router.get("/plans/{plan_id}/revisions", response_model=list[RevisionOut])
async def list_revisions(plan_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await revisions.list_revisions(get_context(), db, plan_id)


@router.get("/plans/{plan_id}/revisions/{revision_no}/diff", response_model=DiffOut)
async def revision_diff(plan_id: uuid.UUID, revision_no: int, _: Read, db: DB) -> Any:
    return await revisions.get_diff(get_context(), db, plan_id, revision_no)


# ================================================================ lines
@router.get("/plans/{plan_id}/lines", response_model=list[LineOut])
async def list_lines(plan_id: uuid.UUID, _: Read, db: DB, revision: int | None = None) -> Any:
    return await lines.list_lines(get_context(), db, plan_id, revision)


@router.post("/plans/{plan_id}/lines", response_model=LineOut, status_code=status.HTTP_201_CREATED)
async def create_line(plan_id: uuid.UUID, body: LineIn, _: Write, db: DB) -> Any:
    return await lines.create_line(get_context(), db, plan_id, body)


@router.post("/plans/{plan_id}/lines/import", response_model=LineImportOut)
async def import_lines(
    plan_id: uuid.UUID, _: Write, db: DB, file: Annotated[UploadFile, File()], apply: bool = False
) -> Any:
    content = await file.read()
    return await imports.import_lines(get_context(), db, plan_id, content, file.filename, apply=apply)


@router.patch("/plans/{plan_id}/lines/{line_id}", response_model=LineOut)
async def update_line(plan_id: uuid.UUID, line_id: uuid.UUID, body: LineUpdate, _: Write, db: DB) -> Any:
    return await lines.update_line(get_context(), db, plan_id, line_id, body)


@router.delete("/plans/{plan_id}/lines/{line_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_line(plan_id: uuid.UUID, line_id: uuid.UUID, _: Write, db: DB) -> Response:
    await lines.delete_line(get_context(), db, plan_id, line_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ================================================================ slots
@router.get("/plans/{plan_id}/slots", response_model=list[SlotOut])
async def list_slots(
    plan_id: uuid.UUID, _: Read, db: DB, status_: Annotated[str | None, Query(alias="status")] = None
) -> Any:
    return await slots.list_slots(get_context(), db, plan_id, status_)


@router.post("/slots/convert", response_model=ConvertOut)
async def convert_slots(body: ConvertIn, _: Write, __: DemandCreate, db: DB) -> Any:
    return await conversion.convert(get_context(), db, body)


@router.post("/slots/{slot_id}/cancel", response_model=SlotOut)
async def cancel_slot(slot_id: uuid.UUID, body: SlotCancelIn, _: Write, db: DB) -> Any:
    return await slots.cancel_slot(get_context(), db, slot_id, body.reason)


# ================================================================ plan vs actual
@router.get("/plans/{plan_id}/actuals", response_model=ActualsOut)
async def plan_vs_actual(plan_id: uuid.UUID, _: Read, db: DB, group_by: GroupBy = "line") -> Any:
    return await actuals.plan_vs_actual(get_context(), db, plan_id, group_by=group_by)


@router.get("/plans/{plan_id}/actuals/export")
async def export_actuals(plan_id: uuid.UUID, _: Read, db: DB, group_by: GroupBy = "line") -> Response:
    content, filename = await actuals.export_xlsx(get_context(), db, plan_id, group_by=group_by)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
