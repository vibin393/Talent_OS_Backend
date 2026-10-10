# ruff: noqa: E501
"""Job boards (Epic 70, spec A8; prefix ``/api/v1``). Publishing, updating, refreshing, closing and the manual path need
``posting:publish_external`` and are user actions (★ in the service: API keys, apps and automated actors get 403
``human_only_action``); reads need ``demand:read``; board settings ``integration:view`` / ``integration:manage``."""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.job_distribution import (
    ApplicantRowOut,
    BoardFieldOut,
    BoardPostingOut,
    BoardReportRow,
    BoardSetIn,
    BoardSetOut,
    BoardSetPatch,
    FeedSettingsIn,
    FeedSettingsOut,
    IdsIn,
    JobBoardSettingsIn,
    JobBoardSettingsOut,
    MappingOut,
    MappingsPut,
    MultiPostIn,
    MultiPostPreviewOut,
    PackOut,
    PublishIn,
    PublishOut,
    RecordIn,
    ReportQuery,
)
from app.services import job_distribution_service as svc

router = APIRouter(tags=["job-boards"])
DB = TenantDBSessionDep
Publish = Annotated[Principal, Depends(require("posting:publish_external"))]
DemandRead = Annotated[Principal, Depends(require("demand:read"))]
AppRead = Annotated[Principal, Depends(require("application:read"))]
View = Annotated[Principal, Depends(require("integration:view"))]
Manage = Annotated[Principal, Depends(require("integration:manage"))]
ReportRead = Annotated[Principal, Depends(require("report:read"))]
Idem = Annotated[IdempotencyGuard, Depends(idempotency())]


# ================================================================ demand board postings
@router.post("/demands/{demand_id}/board-postings/preview", response_model=MultiPostPreviewOut)
async def preview(demand_id: uuid.UUID, body: MultiPostIn, _: Publish, db: DB) -> MultiPostPreviewOut:
    return await svc.preview(get_context(), db, demand_id, body)


@router.post("/demands/{demand_id}/board-postings", response_model=PublishOut, status_code=status.HTTP_202_ACCEPTED)
async def publish(demand_id: uuid.UUID, body: PublishIn, _: Publish, db: DB, idem: Idem) -> Any:
    return await idem.run(lambda: svc.publish(get_context(), db, demand_id, body))


@router.get("/demands/{demand_id}/board-postings", response_model=list[BoardPostingOut])
async def list_postings(demand_id: uuid.UUID, _: DemandRead, db: DB) -> list[BoardPostingOut]:
    return await svc.list_for_demand(get_context(), db, demand_id)


@router.post("/board-postings/update", response_model=list[BoardPostingOut])
async def update(body: IdsIn, _: Publish, db: DB) -> list[BoardPostingOut]:
    return await svc.update(get_context(), db, body.ids)


@router.get("/board-postings/{board_posting_id}", response_model=BoardPostingOut)
async def get(board_posting_id: uuid.UUID, _: DemandRead, db: DB) -> BoardPostingOut:
    return await svc.get(get_context(), db, board_posting_id)


@router.post("/board-postings/{board_posting_id}/refresh", response_model=BoardPostingOut)
async def refresh(board_posting_id: uuid.UUID, _: Publish, db: DB) -> BoardPostingOut:
    return await svc.refresh(get_context(), db, board_posting_id)


@router.post("/board-postings/{board_posting_id}/close", response_model=BoardPostingOut)
async def close(board_posting_id: uuid.UUID, _: Publish, db: DB) -> BoardPostingOut:
    return await svc.close(get_context(), db, board_posting_id)


@router.post("/board-postings/{board_posting_id}/retry", response_model=BoardPostingOut)
async def retry(board_posting_id: uuid.UUID, _: Publish, db: DB) -> BoardPostingOut:
    return await svc.retry(get_context(), db, board_posting_id)


@router.post("/board-postings/{board_posting_id}/abandon", response_model=BoardPostingOut)
async def abandon(board_posting_id: uuid.UUID, _: Publish, db: DB) -> BoardPostingOut:
    return await svc.abandon(get_context(), db, board_posting_id)


@router.get("/board-postings/{board_posting_id}/pack", response_model=PackOut)
async def pack(board_posting_id: uuid.UUID, _: Publish, db: DB) -> PackOut:
    return await svc.pack(get_context(), db, board_posting_id)


@router.post("/board-postings/{board_posting_id}/record", response_model=BoardPostingOut)
async def record(board_posting_id: uuid.UUID, body: RecordIn, _: Publish, db: DB) -> BoardPostingOut:
    return await svc.record(get_context(), db, board_posting_id, body)


@router.post("/board-postings/{board_posting_id}/mark-closed", response_model=BoardPostingOut)
async def mark_closed(board_posting_id: uuid.UUID, _: Publish, db: DB) -> BoardPostingOut:
    return await svc.mark_closed(get_context(), db, board_posting_id)


@router.get("/board-postings/{board_posting_id}/applicants", response_model=list[ApplicantRowOut])
async def applicants(board_posting_id: uuid.UUID, _: AppRead, db: DB) -> list[ApplicantRowOut]:
    return await svc.applicants(get_context(), db, board_posting_id)


# ================================================================ settings
@router.get("/job-boards/settings", response_model=JobBoardSettingsOut)
async def get_settings(_: View, db: DB) -> JobBoardSettingsOut:
    return await svc.get_settings_out(db)


@router.put("/job-boards/settings", response_model=JobBoardSettingsOut)
async def put_settings(body: JobBoardSettingsIn, _: Manage, db: DB) -> JobBoardSettingsOut:
    return await svc.put_settings(db, body)


@router.get("/job-boards/board-sets", response_model=list[BoardSetOut])
async def list_sets(_: View, db: DB) -> list[BoardSetOut]:
    return await svc.list_sets(db)


@router.post("/job-boards/board-sets", response_model=BoardSetOut, status_code=status.HTTP_201_CREATED)
async def create_set(body: BoardSetIn, _: Manage, db: DB) -> BoardSetOut:
    return await svc.create_set(db, body)


@router.patch("/job-boards/board-sets/{set_id}", response_model=BoardSetOut)
async def update_set(set_id: uuid.UUID, body: BoardSetPatch, _: Manage, db: DB) -> BoardSetOut:
    return await svc.update_set(db, set_id, body)


@router.delete("/job-boards/board-sets/{set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_set(set_id: uuid.UUID, _: Manage, db: DB) -> None:
    await svc.delete_set(db, set_id)


@router.get("/job-boards/feed", response_model=FeedSettingsOut)
async def feed_settings(_: Manage, db: DB) -> FeedSettingsOut:
    return await svc.feed_settings(db)


@router.put("/job-boards/feed", response_model=FeedSettingsOut)
async def put_feed_settings(body: FeedSettingsIn, _: Manage, db: DB) -> FeedSettingsOut:
    return await svc.update_feed_settings(db, body)


@router.post("/job-boards/feed/regenerate", response_model=FeedSettingsOut)
async def regenerate_feed(_: Manage, db: DB) -> FeedSettingsOut:
    return await svc.regenerate_feed(db)


@router.get("/job-boards/report", response_model=list[BoardReportRow])
async def report(
    _: ReportRead,
    db: DB,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    board_key: str | None = Query(default=None, max_length=40),
    demand_id: uuid.UUID | None = None,
) -> list[BoardReportRow]:
    q = ReportQuery(from_=from_, to=to, board_key=board_key, demand_id=demand_id)
    return await svc.board_performance(get_context(), db, q)


@router.get("/job-boards/{board_key}/fields", response_model=list[BoardFieldOut])
async def fields(board_key: str, _: Manage, db: DB) -> list[BoardFieldOut]:
    return await svc.board_fields(get_context(), db, board_key)


@router.get("/job-boards/{board_key}/mappings", response_model=list[MappingOut])
async def mappings(board_key: str, _: Manage, db: DB) -> list[MappingOut]:
    return await svc.list_mappings(db, board_key)


@router.put("/job-boards/{board_key}/mappings", response_model=list[MappingOut])
async def put_mappings(board_key: str, body: MappingsPut, _: Manage, db: DB) -> list[MappingOut]:
    return await svc.put_mappings(db, board_key, body)
