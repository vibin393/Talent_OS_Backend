"""Worker "My Work" timesheets (Epic 25; ``/api/v1/portal/work/timesheets``).

The Candidate principal with the worker org scope (AD-27, R17); calls only ``services/time/portal.py`` and
returns
``schemas/time_portal.py`` (hours only: no bill / vendor rate or margin; scanned outside production by the
field-visibility guard). Another Worker's ids -> 404.
"""

import uuid
from typing import Any

from fastapi import APIRouter

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.schemas.time import EntriesIn, SubmitIn
from app.schemas.time_portal import WorkerSplitPreview, WorkerTimesheetOut, WorkerTimesheetSummary
from app.services.time import portal

router = APIRouter(
    prefix="/portal/work/timesheets", tags=["portal-work"], route_class=portal_guard_route("worker")
)


@router.get("", response_model=list[WorkerTimesheetSummary])
async def my_timesheets(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.my_timesheets(get_context(), db, principal.tenant_id, principal.candidate_id)


@router.get("/{timesheet_id}", response_model=WorkerTimesheetOut)
async def my_timesheet(timesheet_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.my_timesheet(
        get_context(), db, principal.tenant_id, principal.candidate_id, timesheet_id
    )


@router.put("/{timesheet_id}/entries", response_model=WorkerTimesheetOut)
async def save_entries(
    timesheet_id: uuid.UUID, data: EntriesIn, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    return await portal.save_entries(
        get_context(), db, principal.tenant_id, principal.candidate_id, timesheet_id, data.days
    )


@router.post("/{timesheet_id}/copy-previous", response_model=WorkerTimesheetOut)
async def copy_previous(timesheet_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.copy_previous(
        get_context(), db, principal.tenant_id, principal.candidate_id, timesheet_id
    )


@router.post("/{timesheet_id}/refresh-prefill", response_model=WorkerTimesheetOut)
async def refresh_prefill(timesheet_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.refresh_prefill(
        get_context(), db, principal.tenant_id, principal.candidate_id, timesheet_id
    )


@router.post("/{timesheet_id}/preview-split", response_model=WorkerSplitPreview)
async def preview_split(timesheet_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.split_preview(
        get_context(), db, principal.tenant_id, principal.candidate_id, timesheet_id
    )


@router.post("/{timesheet_id}/submit", response_model=WorkerTimesheetOut)
async def submit(
    timesheet_id: uuid.UUID, data: SubmitIn, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    return await portal.submit(
        get_context(),
        db,
        principal.tenant_id,
        principal.candidate_id,
        timesheet_id,
        confirm_zero=data.confirm_zero,
    )


@router.post("/{timesheet_id}/recall", response_model=WorkerTimesheetOut)
async def recall(timesheet_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.recall(get_context(), db, principal.tenant_id, principal.candidate_id, timesheet_id)
