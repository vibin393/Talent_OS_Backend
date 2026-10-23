# ruff: file-ignore[line-too-long]
"""Custom report builder & scheduled reports (Epic 47, FR-237..FR-240).

``/reports/custom/*`` (definitions, preview / run / export, shares, pins, run log, settings), ``/me/report-pins``,
``/report-schedules/*`` and ``/report-runs/{id}/download``. The source / field catalogue is the foundation's
``GET /semantic/sources[/{key}]``. Every run executes as the caller (viewer-scoped); each endpoint calls one
service function.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.report_builder import (
    BuilderSettingsIn,
    BuilderSettingsOut,
    ExportIn,
    ExportOut,
    PinOrderIn,
    PinOut,
    PreviewIn,
    ReportCreate,
    ReportOut,
    ReportUpdate,
    RunLogOut,
    RunResultOut,
    ScheduleCreate,
    ScheduleOut,
    SchedulePreviewOut,
    ScheduleUpdate,
    ShareIn,
    ShareOut,
)
from app.services.reporting import builder as svc
from app.services.reporting.builder import schedules as schedule_svc

router = APIRouter(tags=["report-builder"])
DB = TenantDBSessionDep
Paging = Annotated[PageParams, Depends()]
Any_ = Annotated[Principal, Depends(require_any("report:read", "report:build"))]
Build = Annotated[Principal, Depends(require("report:build"))]
Schedule = Annotated[Principal, Depends(require("report:schedule"))]
RunLog = Annotated[Principal, Depends(require("audit:read"))]


# ================================================================ definitions
@router.get("/reports/custom", response_model=Page[ReportOut])
async def list_reports(
    _: Any_,
    db: DB,
    params: Paging,
    tab: Literal["mine", "shared", "all"] = "all",
    q: Annotated[str | None, Query(max_length=100)] = None,
    status_: Annotated[Literal["ok", "needs_repair"] | None, Query(alias="status")] = None,
) -> Any:
    return await svc.list_reports(get_context(), db, params, tab=tab, q=q, status=status_)


@router.post("/reports/custom", response_model=ReportOut, status_code=status.HTTP_201_CREATED)
async def create_report(body: ReportCreate, _: Build, db: DB) -> Any:
    return await svc.create(get_context(), db, body)


@router.post("/reports/custom/preview", response_model=RunResultOut)
async def preview_report(body: PreviewIn, _: Build, db: DB) -> Any:
    return await svc.preview(get_context(), db, body)


@router.get("/reports/custom/runs", response_model=Page[RunLogOut])
async def run_log(
    _: RunLog,
    db: DB,
    params: Paging,
    report_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status", pattern=r"^[a-z_]{1,12}$")] = None,
    run_kind: Annotated[str | None, Query(pattern=r"^(preview|run|export|scheduled)$")] = None,
    user_id: uuid.UUID | None = None,
    since: datetime | None = None,
) -> Any:
    return await svc.list_runs(
        get_context(),
        db,
        params,
        report_id=report_id,
        status=status_,
        run_kind=run_kind,
        user_id=user_id,
        since=since,
    )


@router.get("/reports/custom/settings", response_model=BuilderSettingsOut)
async def get_builder_settings(_: Any_, db: DB) -> Any:
    return await svc.get_settings(get_context(), db)


@router.put("/reports/custom/settings", response_model=BuilderSettingsOut)
async def put_builder_settings(
    body: BuilderSettingsIn, _: Annotated[Principal, Depends(require("report:share_tenant"))], db: DB
) -> Any:
    return await svc.put_settings(get_context(), db, body)


@router.get("/reports/custom/{report_id}", response_model=ReportOut)
async def get_report(report_id: uuid.UUID, _: Any_, db: DB) -> Any:
    return await svc.get(get_context(), db, report_id)


@router.patch("/reports/custom/{report_id}", response_model=ReportOut)
async def update_report(report_id: uuid.UUID, body: ReportUpdate, _: Build, db: DB) -> Any:
    return await svc.update(get_context(), db, report_id, body)


@router.delete("/reports/custom/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_report(report_id: uuid.UUID, _: Build, db: DB) -> Response:
    await svc.archive(get_context(), db, report_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reports/custom/{report_id}/run", response_model=RunResultOut)
async def run_report(
    report_id: uuid.UUID,
    _: Any_,
    db: DB,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    cached: bool = False,
) -> Any:
    return await svc.run_report(get_context(), db, report_id, offset=offset, cached=cached)


@router.post("/reports/custom/{report_id}/export", response_model=ExportOut)
async def export_report(
    report_id: uuid.UUID, body: ExportIn, _: Annotated[Principal, Depends(require("report:export"))], db: DB
) -> Any:
    return await svc.export_report(get_context(), db, report_id, body.format)


@router.put("/reports/custom/{report_id}/shares", response_model=list[ShareOut])
async def put_shares(report_id: uuid.UUID, body: list[ShareIn], _: Build, db: DB) -> Any:
    return await svc.put_shares(get_context(), db, report_id, body)


@router.post("/reports/custom/{report_id}/pin", response_model=list[PinOut])
async def pin_report(report_id: uuid.UUID, _: Any_, db: DB) -> Any:
    return await svc.pin(get_context(), db, report_id)


@router.delete("/reports/custom/{report_id}/pin", response_model=list[PinOut])
async def unpin_report(report_id: uuid.UUID, _: Any_, db: DB) -> Any:
    return await svc.unpin(get_context(), db, report_id)


# ================================================================ pins
@router.get("/me/report-pins", response_model=list[PinOut])
async def my_pins(_: Any_, db: DB) -> Any:
    return await svc.list_pins(get_context(), db)


@router.put("/me/report-pins/order", response_model=list[PinOut])
async def order_pins(body: PinOrderIn, _: Any_, db: DB) -> Any:
    return await svc.order_pins(get_context(), db, body.report_ids)


# ================================================================ schedules
@router.get("/report-schedules", response_model=list[ScheduleOut])
async def list_schedules(_: Schedule, db: DB, report_id: uuid.UUID | None = None) -> Any:
    return await svc.list_schedules(get_context(), db, report_id=report_id)


@router.post("/report-schedules", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(body: ScheduleCreate, _: Schedule, db: DB) -> Any:
    return await svc.create_schedule(get_context(), db, body)


@router.post("/report-schedules/preview", response_model=SchedulePreviewOut)
async def preview_schedule(body: ScheduleCreate, _: Schedule) -> Any:
    return SchedulePreviewOut(next_runs=schedule_svc.preview_next_runs(body))


@router.patch("/report-schedules/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(schedule_id: uuid.UUID, body: ScheduleUpdate, _: Schedule, db: DB) -> Any:
    return await svc.update_schedule(get_context(), db, schedule_id, body)


@router.delete("/report-schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: uuid.UUID, _: Schedule, db: DB) -> Response:
    await svc.delete_schedule(get_context(), db, schedule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/report-schedules/{schedule_id}/resume", response_model=ScheduleOut)
async def resume_schedule(schedule_id: uuid.UUID, _: Schedule, db: DB) -> Any:
    return await svc.resume(get_context(), db, schedule_id)


# ================================================================ downloads
@router.get("/report-runs/{run_id}/download", response_class=Response)
async def download_run(run_id: uuid.UUID, _: Any_, db: DB) -> Response:
    filename, content_type, data = await svc.download(get_context(), db, run_id)
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
