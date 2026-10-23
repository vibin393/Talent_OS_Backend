"""Placements, pre-joining and Worker 360 (Epic 19, FR-104..FR-109).

Permissions: ``placement:read`` (lists, detail, gates, history, touchpoints, tracker), ``placement:create`` (direct
creation), ``placement:update`` (non-commercial PATCH, counter-offer, dropout clear), ``placement:rates`` (rate
segments, margin preview), ``placement:join`` (★ Joined), ``placement:drop``, ``placement:configure`` (settings),
``start_gate:override``, ``worker:read`` (Worker 360; unmask additionally needs ``identity:view``),
``worker_bank:approve`` (HR bank / PAN writes). Rates and margin are masked without ``compensation:view``.
External principals never reach these routes (AD-26). Each endpoint calls one service function.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import COMPENSATION_FIELDS, Principal, mask_compensation, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.pipeline import PublicActionOut
from app.schemas.placements_api import (
    BankAccountIn,
    BankAccountOut,
    DropIn,
    GateOverrideIn,
    GateOverrideOut,
    MarginOut,
    MarginPreviewIn,
    MarkJoinedIn,
    NoteIn,
    PanIn,
    PlacementCreateBody,
    PlacementListItem,
    PlacementOut,
    PlacementSettingsOut,
    PlacementSettingsUpdate,
    PlacementUpdate,
    PreJoiningRow,
    RateSegmentDetailOut,
    RateSegmentIn,
    StartGateOut,
    StatusHistoryOut,
    TouchpointOut,
    UnmaskIn,
    UnmaskOut,
    WorkerOut,
    WorkerProfileOut,
    WorkerProfileUpdate,
)
from app.schemas.placements_portal import JoiningResponseIn
from app.services.placements import (
    creation,
    gates,
    joining,
    lists,
    prejoining,
    rate_segments,
    settings,
    workers,
)
from app.services.placements.common import COMPENSATION_EXTRA

router = APIRouter(prefix="/placements", tags=["placements"])
workers_router = APIRouter(prefix="/workers", tags=["workers"])
public_router = APIRouter(prefix="/public", tags=["public"])
DB = TenantDBSessionDep

Read = Annotated[Principal, Depends(require("placement:read"))]
Create = Annotated[Principal, Depends(require("placement:create"))]
Update = Annotated[Principal, Depends(require("placement:update"))]
Rates = Annotated[Principal, Depends(require("placement:rates"))]
Join = Annotated[Principal, Depends(require("placement:join"))]
Drop = Annotated[Principal, Depends(require("placement:drop"))]
Configure = Annotated[Principal, Depends(require("placement:configure"))]
Override = Annotated[Principal, Depends(require("start_gate:override"))]
WorkerRead = Annotated[Principal, Depends(require("worker:read"))]
BankApprove = Annotated[Principal, Depends(require("worker_bank:approve"))]
_RATE_FIELDS = COMPENSATION_FIELDS | COMPENSATION_EXTRA


def _masked(data: Any, principal: Principal) -> Any:
    return mask_compensation(data, principal, _RATE_FIELDS)


async def _detail(db: Any, placement_id: uuid.UUID) -> PlacementOut:
    return await lists.detail(get_context(), db, placement_id)


# ---------------------------------------------------------------- settings
@router.get("/settings", response_model=PlacementSettingsOut)
async def get_settings(_: Configure, db: DB) -> Any:
    return await settings.get_settings(get_context(), db)


@router.patch("/settings", response_model=PlacementSettingsOut)
async def update_settings(data: PlacementSettingsUpdate, _: Configure, db: DB) -> Any:
    return await settings.update_settings(get_context(), db, data)


# ---------------------------------------------------------------- lists
@router.get("", response_model=Page[PlacementListItem])
async def list_placements(
    _: Read,
    db: DB,
    params: Annotated[PageParams, Depends()],
    client_id: uuid.UUID | None = None,
    legal_entity_id: uuid.UUID | None = None,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    worker_type: str | None = None,
    vendor_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    ending_within_days: Annotated[int | None, Query(ge=1, le=365)] = None,
    compliance: str | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    view: str | None = None,
    filter_key: Annotated[str | None, Query(alias="filter")] = None,
    filter_value: str = "true",
) -> Any:
    return await lists.list_placements(
        get_context(),
        db,
        params,
        client_id=client_id,
        legal_entity_id=legal_entity_id,
        statuses=status_,
        worker_type=worker_type,
        vendor_id=vendor_id,
        candidate_id=candidate_id,
        ending_within_days=ending_within_days,
        compliance=compliance,
        q=q,
        view=view,
        extra={filter_key: filter_value} if filter_key else None,
    )


@router.get("/export", response_class=Response)
async def export_placements(
    _: Read,
    __: Annotated[Principal, Depends(require("report:export"))],
    db: DB,
    client_id: uuid.UUID | None = None,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    worker_type: str | None = None,
    compliance: str | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    view: str | None = None,
) -> Response:
    body = await lists.export_csv(
        get_context(),
        db,
        client_id=client_id,
        statuses=status_,
        worker_type=worker_type,
        compliance=compliance,
        q=q,
        view=view,
    )
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="placements.csv"', "Cache-Control": "no-store"},
    )


@router.get("/pre-joining", response_model=list[PreJoiningRow])
async def pre_joining(_: Read, db: DB, starts_within: Annotated[int, Query(ge=1, le=180)] = 30) -> Any:
    return await lists.pre_joining(get_context(), db, starts_within)


@router.post("/margin-preview", response_model=MarginOut)
async def margin_preview(data: MarginPreviewIn, principal: Rates, db: DB) -> Any:
    return await rate_segments.preview_margin(db, principal.tenant_id, data)  # type: ignore[arg-type]


# ---------------------------------------------------------------- create / read / update
@router.post("", response_model=PlacementOut, status_code=status.HTTP_201_CREATED)
async def create_placement(data: PlacementCreateBody, _: Create, db: DB) -> Any:
    p = await creation.create_direct(get_context(), db, data)
    if data.validate_only:
        return PlacementOut.model_validate(p)
    return await _detail(db, p.id)


@router.get("/{placement_id}", response_model=PlacementOut)
async def get_placement(placement_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await _detail(db, placement_id)


@router.patch("/{placement_id}", response_model=PlacementOut)
async def update_placement(placement_id: uuid.UUID, data: PlacementUpdate, _: Update, db: DB) -> Any:
    await lists.visible(get_context(), db, placement_id)
    await creation.update_placement(get_context(), db, placement_id, data)
    return await _detail(db, placement_id)


@router.get("/{placement_id}/history", response_model=list[StatusHistoryOut])
async def history(placement_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await lists.history(get_context(), db, placement_id)


@router.get("/{placement_id}/touchpoints", response_model=list[TouchpointOut])
async def touchpoints(placement_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await lists.touchpoints(get_context(), db, placement_id)


# ---------------------------------------------------------------- rate segments
@router.get("/{placement_id}/rate-segments", response_model=list[RateSegmentDetailOut])
async def list_rate_segments(placement_id: uuid.UUID, principal: Read, db: DB) -> Any:
    await lists.visible(get_context(), db, placement_id)
    rows = await rate_segments.list_segments(get_context(), db, placement_id)
    return _masked([r.model_dump() for r in rows], principal)


@router.post(
    "/{placement_id}/rate-segments", response_model=RateSegmentDetailOut, status_code=status.HTTP_201_CREATED
)
async def add_rate_segment(placement_id: uuid.UUID, data: RateSegmentIn, principal: Rates, db: DB) -> Any:
    await lists.visible(get_context(), db, placement_id)
    out = await rate_segments.add_segment(get_context(), db, placement_id, data)
    return _masked(out, principal)


# ---------------------------------------------------------------- start gates
@router.get("/{placement_id}/start-gates", response_model=list[StartGateOut])
async def list_gates(placement_id: uuid.UUID, _: Read, db: DB) -> Any:
    await lists.visible(get_context(), db, placement_id)
    return await gates.list_gates(get_context(), db, placement_id)


@router.post("/{placement_id}/start-gates/refresh", response_model=list[StartGateOut])
async def refresh_gates(placement_id: uuid.UUID, _: Read, db: DB) -> Any:
    p = await lists.get_or_404(get_context(), db, placement_id)
    await gates.refresh(get_context(), db, p)
    return await gates.list_gates(get_context(), db, placement_id)


@router.get("/{placement_id}/gate-overrides", response_model=list[GateOverrideOut])
async def list_overrides(placement_id: uuid.UUID, _: Read, db: DB) -> Any:
    await lists.visible(get_context(), db, placement_id)
    return await gates.list_overrides(get_context(), db, placement_id)


@router.post(
    "/{placement_id}/gate-overrides", response_model=GateOverrideOut, status_code=status.HTTP_201_CREATED
)
async def request_override(placement_id: uuid.UUID, data: GateOverrideIn, _: Override, db: DB) -> Any:
    await lists.visible(get_context(), db, placement_id)
    return await gates.request_override(get_context(), db, placement_id, data.gate_ids, data.reason)


# ---------------------------------------------------------------- lifecycle
@router.post("/{placement_id}/join", response_model=PlacementOut)
async def mark_joined(placement_id: uuid.UUID, data: MarkJoinedIn, _: Join, db: DB) -> Any:
    await lists.visible(get_context(), db, placement_id)
    await joining.mark_joined(get_context(), db, placement_id, data.actual_start_date)
    return await _detail(db, placement_id)


@router.post("/{placement_id}/drop", response_model=PlacementOut)
async def drop(placement_id: uuid.UUID, data: DropIn, _: Drop, db: DB) -> Any:
    await lists.visible(get_context(), db, placement_id)
    await joining.drop(get_context(), db, placement_id, data.reason, data.note)
    return await _detail(db, placement_id)


@router.post("/{placement_id}/counter-offer", response_model=PlacementOut)
async def counter_offer(placement_id: uuid.UUID, data: NoteIn, _: Update, db: DB) -> Any:
    await lists.visible(get_context(), db, placement_id)
    await joining.record_counter_offer(get_context(), db, placement_id, data.note)
    return await _detail(db, placement_id)


@router.post("/{placement_id}/dropout-risk/clear", response_model=PlacementOut)
async def clear_dropout(placement_id: uuid.UUID, data: NoteIn, _: Update, db: DB) -> Any:
    await lists.visible(get_context(), db, placement_id)
    await joining.clear_dropout(get_context(), db, placement_id, data.note)
    return await _detail(db, placement_id)


# ---------------------------------------------------------------- workers
@workers_router.get("/{candidate_id}", response_model=WorkerOut)
async def get_worker(candidate_id: uuid.UUID, _: WorkerRead, db: DB) -> Any:
    return await workers.get_worker(get_context(), db, candidate_id)


@workers_router.post("/{candidate_id}/unmask", response_model=UnmaskOut)
async def unmask(candidate_id: uuid.UUID, data: UnmaskIn, response: Response, _: WorkerRead, db: DB) -> Any:
    response.headers["Cache-Control"] = "no-store"
    return await workers.unmask_field(get_context(), db, candidate_id, data.field, data.bank_account_id)


@workers_router.patch("/{candidate_id}/profile", response_model=WorkerProfileOut)
async def update_worker_profile(
    candidate_id: uuid.UUID, data: WorkerProfileUpdate, _: WorkerRead, db: DB
) -> Any:
    return await workers.update_profile(get_context(), db, candidate_id, data)


@workers_router.post("/{candidate_id}/pan", response_model=WorkerProfileOut)
async def record_pan(candidate_id: uuid.UUID, data: PanIn, _: BankApprove, db: DB) -> Any:
    return await workers.record_pan(get_context(), db, candidate_id, data.pan, data.pan_name)


@workers_router.post("/{candidate_id}/pan/confirm", response_model=WorkerProfileOut)
async def confirm_pan(candidate_id: uuid.UUID, _: BankApprove, db: DB) -> Any:
    return await workers.confirm_pan(get_context(), db, candidate_id)


@workers_router.post(
    "/{candidate_id}/bank-accounts", response_model=BankAccountOut, status_code=status.HTTP_201_CREATED
)
async def request_bank_change(candidate_id: uuid.UUID, data: BankAccountIn, _: BankApprove, db: DB) -> Any:
    row = await workers.request_bank_change(get_context(), db, candidate_id, data, "user")
    return BankAccountOut.model_validate(row)


# ---------------------------------------------------------------- public joining-confirmation link
@public_router.post("/links/{token}/joining", response_model=PublicActionOut)
async def joining_response(token: str, data: JoiningResponseIn) -> Any:
    return PublicActionOut(status=await prejoining.link_respond(token, data))
