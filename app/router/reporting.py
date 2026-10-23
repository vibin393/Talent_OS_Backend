"""Reporting endpoints (Epic 11): dashboards / tiles, report lists (drill-down targets) and exports.
Each endpoint calls one service function; the export endpoint only turns the result into a response."""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.reporting import (
    ApplicationListFilters,
    DashboardListOut,
    ExecGridOut,
    ExportAccepted,
    ExportCreate,
    ExportOut,
    InterviewListFilters,
    OfferListFilters,
    P2ListKind,
    ReconciliationOut,
    ReportListOut,
    SubmissionListFilters,
    TileOut,
)
from app.services import reporting_service as svc

router = APIRouter(tags=["reporting"])
exports_router = APIRouter(prefix="/exports", tags=["exports"])

ReportRead = Annotated[Principal, Depends(require("report:read"))]
# Epic 65: the "AI agents" dashboard is gated by ai:govern (AI Administrators hold no report:read).
DashboardRead = Annotated[Principal, Depends(require_any("report:read", "ai:govern"))]
ReportExport = Annotated[Principal, Depends(require("report:export"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep
Segment = Annotated[str, Path(pattern=r"^[a-z0-9_]{1,40}$")]


# ================================================================ dashboards
@router.get("/dashboards", response_model=DashboardListOut)
async def list_dashboards(_: DashboardRead, db: DB) -> Any:
    return await svc.list_dashboards(get_context(), db)


@router.get("/dashboards/{dashboard}/tiles/{tile_key}", response_model=TileOut)
async def get_tile(
    dashboard: Segment,
    tile_key: Segment,
    _: DashboardRead,
    db: DB,
    response: Response,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    business_unit_ids: Annotated[list[uuid.UUID] | None, Query()] = None,
    granularity: Annotated[str | None, Query(pattern=r"^(day|week|month)$")] = None,
    refresh: bool = False,
    legal_entity_ids: Annotated[list[uuid.UUID] | None, Query()] = None,
    as_of: date | None = None,
    agent_key: Annotated[str | None, Query(max_length=40, pattern=r"^[a-z_]+$")] = None,
) -> Any:
    response.headers["Cache-Control"] = "no-store"
    return await svc.get_tile(
        get_context(),
        db,
        dashboard,
        tile_key,
        date_from=date_from,
        date_to=date_to,
        business_unit_ids=business_unit_ids,
        granularity=granularity,
        refresh=refresh,
        legal_entity_ids=legal_entity_ids,
        as_of=as_of,
        agent_key=agent_key,
    )


# ================================================================ Epic 34: executive grid, reconciliation
ExecutiveRead = Annotated[Principal, Depends(require("executive_dashboard:read"))]
ReconRead = Annotated[Principal, Depends(require_any("executive_dashboard:read", "report:finance_export"))]


@router.get("/dashboards/executive/grid", response_model=ExecGridOut)
async def executive_grid(
    _: ReportRead,
    __: ExecutiveRead,
    db: DB,
    response: Response,
    legal_entity_id: uuid.UUID | None = None,
    month_from: date | None = None,
    month_to: date | None = None,
) -> Any:
    response.headers["Cache-Control"] = "no-store"
    return await svc.executive_grid(
        get_context(), db, legal_entity_id=legal_entity_id, month_from=month_from, month_to=month_to
    )


@router.get("/reports/reconciliation", response_model=ReconciliationOut)
async def reconciliation(_: ReconRead, db: DB) -> Any:
    return await svc.reconciliation_issues(get_context(), db)


# ================================================================ report lists
@router.get("/reports/lists/applications", response_model=ReportListOut)
async def list_applications(
    _: ReportRead, db: DB, filters: Annotated[ApplicationListFilters, Query()], params: Paging
) -> Any:
    return await svc.list_report(get_context(), db, "applications", filters, params)


@router.get("/reports/lists/submissions", response_model=ReportListOut)
async def list_submissions(
    _: ReportRead, db: DB, filters: Annotated[SubmissionListFilters, Query()], params: Paging
) -> Any:
    return await svc.list_report(get_context(), db, "submissions", filters, params)


@router.get("/reports/lists/interviews", response_model=ReportListOut)
async def list_interviews(
    _: ReportRead, db: DB, filters: Annotated[InterviewListFilters, Query()], params: Paging
) -> Any:
    return await svc.list_report(get_context(), db, "interviews", filters, params)


@router.get("/reports/lists/offers", response_model=ReportListOut)
async def list_offers(
    _: ReportRead, db: DB, filters: Annotated[OfferListFilters, Query()], params: Paging
) -> Any:
    return await svc.list_report(get_context(), db, "offers", filters, params)


@router.get("/reports/lists/{kind}", response_model=ReportListOut)
async def list_p2_kind(kind: P2ListKind, _: ReportRead, db: DB, request: Request, params: Paging) -> Any:
    """Epic 34 (A4): the Phase 2 list kinds (``lists_p2``), with the P1 list envelope."""
    from app.services.reporting import lists

    paging = {"page", "page_size"}
    raw: dict[str, Any] = {}
    for key in request.query_params:
        if key in paging:
            continue
        values = request.query_params.getlist(key)
        raw[key] = values if key == "legal_entity_ids" else values[-1]
    filters = lists.parse_filters(kind, raw)
    return await svc.list_report(get_context(), db, kind, filters, params)


# ================================================================ exports
def _content_disposition(filename: str) -> str:
    return f'attachment; filename="{filename}"'


@exports_router.post(
    "",
    response_model=None,
    status_code=status.HTTP_200_OK,
    responses={
        200: {
            "description": "Small export: the file itself",
            "content": {"text/csv": {}, "application/octet-stream": {}},
        },
        202: {"model": ExportAccepted, "description": "Large export queued; notified when ready"},
    },
)
async def create_export(body: ExportCreate, _: ReportExport, db: DB) -> Response:
    result = await svc.request_export(get_context(), db, body)
    masked = ",".join(result.masked_columns)
    headers = {
        "X-Export-Id": str(result.export.id),
        "X-Export-Rows": str(result.export.row_count or 0),
        "X-TOS-Masked-Columns": masked,
    }
    if result.mode == "async":
        accepted = ExportAccepted(
            export_id=result.export.id,
            status=result.export.status,
            row_count=result.export.row_count or 0,
            masked_columns=result.masked_columns,
        )
        return JSONResponse(
            accepted.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED, headers=headers
        )
    headers.update(
        {
            "Content-Disposition": _content_disposition(result.filename),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
    )
    if result.stream is not None:
        return StreamingResponse(result.stream, media_type=result.content_type, headers=headers)
    return Response(content=result.body or b"", media_type=result.content_type, headers=headers)


@exports_router.get("", response_model=Page[ExportOut])
async def list_exports(_: ReportExport, db: DB, params: Paging) -> Any:
    return await svc.list_exports(get_context(), db, params)


@exports_router.get("/{export_id}", response_model=ExportOut)
async def get_export(export_id: uuid.UUID, _: ReportExport, db: DB) -> Any:
    return await svc.get_export(get_context(), db, export_id)


@exports_router.get(
    "/{export_id}/download", response_class=RedirectResponse, status_code=status.HTTP_303_SEE_OTHER
)
async def download_export(export_id: uuid.UUID, _: ReportExport, db: DB) -> RedirectResponse:
    file_id = await svc.download_export(get_context(), db, export_id)
    return RedirectResponse(
        f"/api/v1/files/{file_id}/download",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store"},
    )
