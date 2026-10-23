# ruff: file-ignore[line-too-long]
"""Sales CRM endpoints (Epic 15, FR-83..FR-87): ``/crm/*``, ``/leads``, ``/lead-imports``, ``/opportunities``,
``/clients/{id}/opportunities``.

Permissions: leads ``lead:read|write``; opportunities ``opportunity:read|write``; the dashboard needs both reads;
settings read with ``lead:read`` or ``opportunity:read`` and written with ``settings:manage``. Data scope
(own|team|bu|tenant) applies on ``owner_id``. External (client / vendor) principals never reach these routes.
Each endpoint calls one service function. The Won conversion requires an ``Idempotency-Key``.
"""

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.crm import (
    BoardOut,
    ClientOpportunitiesOut,
    CrmSettingsOut,
    CrmSettingsUpdate,
    DashboardOut,
    DuplicateCheckIn,
    DuplicateMatch,
    LeadConvertIn,
    LeadConvertOut,
    LeadCreate,
    LeadImportOut,
    LeadOut,
    LeadPage,
    LeadSourceIn,
    LeadSourceOut,
    LeadSourceUpdate,
    LeadStatus,
    LeadTransitionIn,
    LeadUpdate,
    OpportunityCreate,
    OpportunityLinesIn,
    OpportunityOut,
    OpportunityPage,
    OpportunityStage,
    OpportunityTransitionIn,
    OpportunityUpdate,
    ReasonCodeIn,
    ReasonCodeOut,
    ReasonCodeUpdate,
    ReasonKind,
    RecordKind,
    RecordLinkOut,
    Service,
    StatusHistoryOut,
    SummaryOut,
    WinIn,
    WinOut,
    WonPreviewOut,
)
from app.services import crm_service

router = APIRouter(tags=["crm"])
DB = TenantDBSessionDep

LeadRead = Annotated[Principal, Depends(require("lead:read"))]
LeadWrite = Annotated[Principal, Depends(require("lead:write"))]
OppRead = Annotated[Principal, Depends(require("opportunity:read"))]
OppWrite = Annotated[Principal, Depends(require("opportunity:write"))]
CrmRead = Annotated[Principal, Depends(require_any("lead:read", "opportunity:read"))]
SettingsManage = Annotated[Principal, Depends(require("settings:manage"))]


def _ids(values: list[str] | None) -> list[uuid.UUID] | None:
    if not values:
        return None
    try:
        return [uuid.UUID(v.strip()) for raw in values for v in raw.split(",") if v.strip()]
    except ValueError as exc:
        from app.core.errors import ValidationFailed

        raise ValidationFailed("owner_ids must be user ids", code="validation_error") from exc


# ================================================================ settings, reason codes, sources
@router.get("/crm/settings", response_model=CrmSettingsOut)
async def get_settings(_: CrmRead, db: DB) -> CrmSettingsOut:
    return await crm_service.settings.get_settings(get_context(), db)


@router.patch("/crm/settings", response_model=CrmSettingsOut)
async def update_settings(body: CrmSettingsUpdate, _: SettingsManage, db: DB) -> CrmSettingsOut:
    return await crm_service.settings.update_settings(get_context(), db, body)


@router.get("/crm/reason-codes", response_model=list[ReasonCodeOut])
async def list_reason_codes(
    _: CrmRead, db: DB, kind: ReasonKind | None = None, include_archived: bool = False
) -> list[ReasonCodeOut]:
    return await crm_service.settings.list_reason_codes(get_context(), db, kind, include_archived)


@router.post("/crm/reason-codes", response_model=ReasonCodeOut, status_code=status.HTTP_201_CREATED)
async def create_reason_code(body: ReasonCodeIn, _: SettingsManage, db: DB) -> ReasonCodeOut:
    return await crm_service.settings.create_reason_code(get_context(), db, body)


@router.patch("/crm/reason-codes/{code_id}", response_model=ReasonCodeOut)
async def update_reason_code(
    code_id: uuid.UUID, body: ReasonCodeUpdate, _: SettingsManage, db: DB
) -> ReasonCodeOut:
    return await crm_service.settings.update_reason_code(get_context(), db, code_id, body)


@router.get("/crm/lead-sources", response_model=list[LeadSourceOut])
async def list_sources(_: CrmRead, db: DB, include_archived: bool = False) -> list[LeadSourceOut]:
    return await crm_service.settings.list_sources(get_context(), db, include_archived)


@router.post("/crm/lead-sources", response_model=LeadSourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(body: LeadSourceIn, _: SettingsManage, db: DB) -> LeadSourceOut:
    return await crm_service.settings.create_source(get_context(), db, body)


@router.patch("/crm/lead-sources/{source_id}", response_model=LeadSourceOut)
async def update_source(
    source_id: uuid.UUID, body: LeadSourceUpdate, _: SettingsManage, db: DB
) -> LeadSourceOut:
    return await crm_service.settings.update_source(get_context(), db, source_id, body)


@router.get("/crm/links", response_model=RecordLinkOut | None)
async def record_link(
    _: OppRead, db: DB, record_kind: RecordKind, record_id: uuid.UUID
) -> RecordLinkOut | None:
    return await crm_service.record_link(get_context(), db, record_kind, record_id)


@router.get("/crm/dashboard", response_model=DashboardOut)
async def dashboard(
    _: OppRead,
    __: LeadRead,
    db: DB,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    owner_ids: Annotated[list[str] | None, Query(description="repeated or comma-separated user ids")] = None,
    team_id: uuid.UUID | None = None,
) -> DashboardOut:
    return await crm_service.dashboard.dashboard(
        get_context(), db, from_date=from_, to_date=to, owner_ids=_ids(owner_ids), team_id=team_id
    )


# ================================================================ leads
@router.get("/leads", response_model=LeadPage)
async def list_leads(
    _: LeadRead,
    db: DB,
    status_: Annotated[list[LeadStatus] | None, Query(alias="status")] = None,
    owner_id: uuid.UUID | None = None,
    source: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    created_from: date | None = None,
    created_to: date | None = None,
    include_archived: bool = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
) -> LeadPage:
    return await crm_service.leads.list_leads(
        get_context(), db, status=status_, owner_id=owner_id, source=source, q=q, created_from=created_from,  # type: ignore[arg-type]
        created_to=created_to, include_archived=include_archived, page=page, page_size=page_size,
    )  # fmt: skip


@router.post("/leads", response_model=LeadOut, status_code=status.HTTP_201_CREATED)
async def create_lead(body: LeadCreate, _: LeadWrite, db: DB) -> LeadOut:
    return await crm_service.leads.create(get_context(), db, body)


@router.post("/leads/duplicate-check", response_model=list[DuplicateMatch])
async def duplicate_check(body: DuplicateCheckIn, _: LeadRead, db: DB) -> list[DuplicateMatch]:
    return await crm_service.find_duplicates(
        get_context(), db, body.company_name, body.website, body.gstin, body.exclude_lead_id
    )


@router.get("/leads/import-template.csv")
async def import_template(_: LeadWrite) -> Response:
    return Response(
        content=crm_service.imports.TEMPLATE,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads-import-template.csv"'},
    )


@router.post("/leads/import", response_model=LeadImportOut, status_code=status.HTTP_202_ACCEPTED)
async def import_leads(
    _: LeadWrite,
    db: DB,
    file: Annotated[UploadFile, File()],
    owner_id: Annotated[uuid.UUID | None, Form()] = None,
    default_source: Annotated[str | None, Form()] = None,
    skip_duplicates: Annotated[bool, Form()] = True,
) -> LeadImportOut:
    data = await file.read(crm_service.imports.MAX_BYTES + 1)
    return await crm_service.imports.start_import(
        get_context(),
        db,
        data,
        owner_id=owner_id,
        default_source=default_source or None,
        skip_duplicates=skip_duplicates,
    )


@router.get("/lead-imports/{batch_id}", response_model=LeadImportOut)
async def get_import(batch_id: uuid.UUID, _: LeadWrite, db: DB) -> LeadImportOut:
    return await crm_service.imports.get_import(get_context(), db, batch_id)


@router.get("/lead-imports/{batch_id}/errors.csv")
async def import_errors(batch_id: uuid.UUID, _: LeadWrite, db: DB) -> Response:
    body = await crm_service.imports.errors_csv(get_context(), db, batch_id)
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="lead-import-errors.csv"'},
    )


@router.get("/leads/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: uuid.UUID, _: LeadRead, db: DB) -> LeadOut:
    return await crm_service.leads.get_lead(get_context(), db, lead_id)


@router.patch("/leads/{lead_id}", response_model=LeadOut)
async def update_lead(lead_id: uuid.UUID, body: LeadUpdate, _: LeadWrite, db: DB) -> LeadOut:
    return await crm_service.leads.update_lead(get_context(), db, lead_id, body)


@router.post("/leads/{lead_id}/transition", response_model=LeadOut)
async def transition_lead(lead_id: uuid.UUID, body: LeadTransitionIn, _: LeadWrite, db: DB) -> LeadOut:
    return await crm_service.transition_lead(get_context(), db, lead_id, body)


@router.post("/leads/{lead_id}/archive", response_model=LeadOut)
async def archive_lead(lead_id: uuid.UUID, _: LeadWrite, db: DB) -> LeadOut:
    return await crm_service.leads.archive_lead(get_context(), db, lead_id)


@router.get("/leads/{lead_id}/history", response_model=list[StatusHistoryOut])
async def lead_history(lead_id: uuid.UUID, _: LeadRead, db: DB) -> list[StatusHistoryOut]:
    return await crm_service.leads.lead_history(get_context(), db, lead_id)


@router.post("/leads/{lead_id}/convert", response_model=LeadConvertOut, status_code=status.HTTP_201_CREATED)
async def convert_lead(
    lead_id: uuid.UUID, body: LeadConvertIn, _: LeadWrite, __: OppWrite, db: DB
) -> LeadConvertOut:
    return await crm_service.convert_lead(get_context(), db, lead_id, body)


# ================================================================ opportunities
def _opp_filters(
    stage: Annotated[list[OpportunityStage] | None, Query()] = None,
    owner_id: uuid.UUID | None = None,
    account_manager_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    close_from: date | None = None,
    close_to: date | None = None,
    service: Service | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    return {
        "stage": stage, "owner_id": owner_id, "account_manager_id": account_manager_id, "client_id": client_id,
        "close_from": close_from, "close_to": close_to, "service": service, "q": q, "include_archived": include_archived,
    }  # fmt: skip


OppFilters = Annotated[dict[str, Any], Depends(_opp_filters)]


@router.get("/opportunities", response_model=OpportunityPage)
async def list_opportunities(
    _: OppRead,
    db: DB,
    filters: OppFilters,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
) -> OpportunityPage:
    return await crm_service.opportunities.list_opportunities(get_context(), db, filters, page, page_size)


@router.get("/opportunities/board", response_model=BoardOut)
async def board(_: OppRead, db: DB, filters: OppFilters) -> BoardOut:
    return await crm_service.opportunities.board(get_context(), db, filters)


@router.get("/opportunities/summary", response_model=SummaryOut)
async def summary(
    _: OppRead, db: DB, filters: OppFilters, group_by: Literal["owner", "stage", "close_month"] = "stage"
) -> SummaryOut:
    return await crm_service.opportunities.summary(get_context(), db, group_by, filters)


@router.post("/opportunities", response_model=OpportunityOut, status_code=status.HTTP_201_CREATED)
async def create_opportunity(body: OpportunityCreate, _: OppWrite, db: DB) -> OpportunityOut:
    return await crm_service.opportunities.create(get_context(), db, body)


@router.get("/opportunities/{opp_id}", response_model=OpportunityOut)
async def get_opportunity(opp_id: uuid.UUID, _: OppRead, db: DB) -> OpportunityOut:
    return await crm_service.opportunities.get_opportunity(get_context(), db, opp_id)


@router.patch("/opportunities/{opp_id}", response_model=OpportunityOut)
async def update_opportunity(
    opp_id: uuid.UUID, body: OpportunityUpdate, _: OppWrite, db: DB
) -> OpportunityOut:
    return await crm_service.opportunities.update_opportunity(get_context(), db, opp_id, body)


@router.put("/opportunities/{opp_id}/lines", response_model=OpportunityOut)
async def replace_lines(opp_id: uuid.UUID, body: OpportunityLinesIn, _: OppWrite, db: DB) -> OpportunityOut:
    return await crm_service.opportunities.replace_lines(get_context(), db, opp_id, body.lines)


@router.post("/opportunities/{opp_id}/transition", response_model=OpportunityOut)
async def transition_opportunity(
    opp_id: uuid.UUID, body: OpportunityTransitionIn, _: OppWrite, db: DB
) -> OpportunityOut:
    return await crm_service.transition_opportunity(get_context(), db, opp_id, body)


@router.post("/opportunities/{opp_id}/reset-probability", response_model=OpportunityOut)
async def reset_probability(opp_id: uuid.UUID, _: OppWrite, db: DB) -> OpportunityOut:
    return await crm_service.opportunities.reset_probability(get_context(), db, opp_id)


@router.get("/opportunities/{opp_id}/history", response_model=list[StatusHistoryOut])
async def opportunity_history(opp_id: uuid.UUID, _: OppRead, db: DB) -> list[StatusHistoryOut]:
    return await crm_service.opportunities.opportunity_history(get_context(), db, opp_id)


@router.post("/opportunities/{opp_id}/won-preview", response_model=WonPreviewOut)
async def won_preview(opp_id: uuid.UUID, _: OppWrite, db: DB) -> WonPreviewOut:
    return await crm_service.won_preview(get_context(), db, opp_id)


@router.post("/opportunities/{opp_id}/win", response_model=WinOut)
async def win(
    opp_id: uuid.UUID,
    body: WinIn,
    _: OppWrite,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
) -> Any:
    return await idem.run(lambda: crm_service.win_opportunity(get_context(), db, opp_id, body))


@router.get("/clients/{client_id}/opportunities", response_model=ClientOpportunitiesOut)
async def client_opportunities(client_id: uuid.UUID, _: OppRead, db: DB) -> ClientOpportunitiesOut:
    return await crm_service.opportunities.client_opportunities(get_context(), db, client_id)
