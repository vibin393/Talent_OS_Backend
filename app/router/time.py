"""Timesheets, internal app (Epic 25, FR-128..FR-133): ``/api/v1/time``.

Permissions: ``timesheet:read`` (lists, detail), ``timesheet:configure`` (settings, Client defaults, Placement
configs), ``timesheet:admin`` (proxy entry / submit, generate, import, reopen, adjustments, offline Client
approval), ``timesheet:monitor`` (monitor, reminders). Approver decisions go through ``/approvals`` (the
approvals engine, AD-24). Unbilled values are masked without ``compensation:view``. Each endpoint calls one
service function.
"""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.time import (
    AdjustmentIn,
    ClientTimeDefaultsOut,
    ImportApplyOut,
    ImportPreviewOut,
    MonitorOut,
    ProxyEntriesIn,
    ProxySubmitIn,
    ReasonIn,
    RemindIn,
    RemindOut,
    TimeConfigIn,
    TimeSettingsOut,
    TimeSettingsUpdate,
    TimesheetConfigOut,
    TimesheetDetailOut,
    TimesheetSummaryOut,
)
from app.services import files_service
from app.services.time import adjustments, approval, configs, imports, monitoring
from app.services.time import timesheets as ts_service
from app.services.time.common import get_placement, get_timesheet

router = APIRouter(prefix="/time", tags=["timesheets"])
DB = TenantDBSessionDep

Read = Annotated[Principal, Depends(require("timesheet:read"))]
Configure = Annotated[Principal, Depends(require("timesheet:configure"))]
Admin = Annotated[Principal, Depends(require("timesheet:admin"))]
Monitor = Annotated[Principal, Depends(require("timesheet:monitor"))]


# ---------------------------------------------------------------- settings / configuration
@router.get("/settings", response_model=TimeSettingsOut)
async def get_settings(_: Configure, db: DB) -> Any:
    return await configs.get_settings(get_context(), db)


@router.patch("/settings", response_model=TimeSettingsOut)
async def update_settings(data: TimeSettingsUpdate, _: Configure, db: DB) -> Any:
    return await configs.update_settings(get_context(), db, data)


@router.get("/client-defaults/{client_id}", response_model=ClientTimeDefaultsOut)
async def get_client_defaults(client_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await configs.get_client_defaults(get_context(), db, client_id)


@router.put("/client-defaults/{client_id}", response_model=ClientTimeDefaultsOut)
async def put_client_defaults(client_id: uuid.UUID, data: TimeConfigIn, _: Configure, db: DB) -> Any:
    return await configs.put_client_defaults(get_context(), db, client_id, data)


@router.get("/placements/{placement_id}/config", response_model=TimesheetConfigOut | list[TimesheetConfigOut])
async def get_config(
    placement_id: uuid.UUID, _: Read, db: DB, history: Annotated[bool, Query()] = False
) -> Any:
    if history:
        return await configs.config_history(get_context(), db, placement_id)
    return await configs.get_config(get_context(), db, placement_id)


@router.put("/placements/{placement_id}/config", response_model=TimesheetConfigOut)
async def put_config(placement_id: uuid.UUID, data: TimeConfigIn, _: Configure, db: DB) -> Any:
    return await configs.update_config(get_context(), db, placement_id, data)


@router.post("/placements/{placement_id}/timesheets/generate", response_model=list[TimesheetSummaryOut])
async def generate(placement_id: uuid.UUID, _: Admin, db: DB) -> Any:
    placement = await get_placement(db, placement_id)
    created = await ts_service.create_period_timesheets(get_context(), db, placement)
    return await ts_service.summaries(db, created)


# ---------------------------------------------------------------- timesheets
@router.get("/timesheets", response_model=list[TimesheetSummaryOut])
async def list_timesheets(
    _: Read,
    db: DB,
    placement_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
    period_start: date | None = None,
) -> Any:
    return await ts_service.list_timesheets(
        get_context(), db, placement_id=placement_id, status=status_, period_start=period_start
    )


@router.get("/timesheets/{timesheet_id}", response_model=TimesheetDetailOut)
async def get_timesheet_detail(timesheet_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await ts_service.detail(get_context(), db, timesheet_id)


@router.put("/timesheets/{timesheet_id}/entries", response_model=TimesheetDetailOut)
async def proxy_entries(timesheet_id: uuid.UUID, data: ProxyEntriesIn, _: Admin, db: DB) -> Any:
    ts = await get_timesheet(db, timesheet_id, lock=True)
    await ts_service.upsert_entries(get_context(), db, ts, data.days, proxy=True)
    ts.proxy_reason = data.reason
    await db.flush()
    return await ts_service.detail(get_context(), db, timesheet_id)


@router.post("/timesheets/{timesheet_id}/submit", response_model=TimesheetDetailOut)
async def proxy_submit(timesheet_id: uuid.UUID, data: ProxySubmitIn, _: Admin, db: DB) -> Any:
    ts = await get_timesheet(db, timesheet_id, lock=True)
    await ts_service.submit(get_context(), db, ts, confirm_zero=data.confirm_zero, proxy_reason=data.reason)
    return await ts_service.detail(get_context(), db, timesheet_id)


@router.post("/timesheets/{timesheet_id}/reopen", response_model=TimesheetDetailOut)
async def reopen(timesheet_id: uuid.UUID, data: ReasonIn, _: Admin, db: DB) -> Any:
    ts = await get_timesheet(db, timesheet_id, lock=True)
    await ts_service.reopen(get_context(), db, ts, data.reason)
    return await ts_service.detail(get_context(), db, timesheet_id)


@router.post(
    "/timesheets/{timesheet_id}/adjustments",
    response_model=TimesheetDetailOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_adjustment(timesheet_id: uuid.UUID, data: AdjustmentIn, _: Admin, db: DB) -> Any:
    adj = await adjustments.create_adjustment(get_context(), db, timesheet_id, data)
    return await ts_service.detail(get_context(), db, adj.id)


@router.post("/timesheets/{timesheet_id}/record-client-approval", response_model=TimesheetDetailOut)
async def record_client_approval(
    timesheet_id: uuid.UUID,
    _: Admin,
    db: DB,
    file: Annotated[UploadFile, File()],
    client_user_id: Annotated[uuid.UUID | None, Form()] = None,
    comment: Annotated[str | None, Form(max_length=4000)] = None,
) -> Any:
    data = await files_service.read_upload(file)
    await approval.record_client_approval(
        get_context(),
        db,
        timesheet_id,
        data=data,
        filename=file.filename,
        client_user_id=client_user_id,
        comment=comment,
    )
    return await ts_service.detail(get_context(), db, timesheet_id)


# ---------------------------------------------------------------- monitor (FR-133)
@router.get("/monitor", response_model=MonitorOut)
async def monitor(
    _: Monitor,
    db: DB,
    period_start: date | None = None,
    legal_entity_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
) -> Any:
    return await monitoring.monitor(
        get_context(),
        db,
        period_start=period_start,
        legal_entity_id=legal_entity_id,
        client_id=client_id,
        status=status_,
    )


@router.post("/monitor/remind", response_model=RemindOut)
async def remind(data: RemindIn, _: Monitor, db: DB) -> Any:
    return await monitoring.remind(get_context(), db, data.timesheet_ids)


# ---------------------------------------------------------------- import (FR-131)
@router.post("/imports", response_model=ImportPreviewOut, status_code=status.HTTP_201_CREATED)
async def import_preview(
    _: Admin,
    db: DB,
    file: Annotated[UploadFile, File()],
    mark_client_approved: Annotated[bool, Form()] = False,
) -> Any:
    data = await files_service.read_upload(file)
    return await imports.preview(
        get_context(), db, data, file.filename, mark_client_approved=mark_client_approved
    )


@router.get("/imports/{batch_id}", response_model=ImportPreviewOut)
async def import_get(batch_id: uuid.UUID, _: Admin, db: DB) -> Any:
    return await imports.get_batch(get_context(), db, batch_id)


@router.post("/imports/{batch_id}/apply", response_model=ImportApplyOut)
async def import_apply(
    batch_id: uuid.UUID, _: Admin, db: DB, idem: Annotated[IdempotencyGuard, Depends(idempotency())]
) -> Any:
    return await idem.run(lambda: imports.apply(get_context(), db, batch_id))
