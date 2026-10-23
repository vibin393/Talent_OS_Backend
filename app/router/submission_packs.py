# ruff: file-ignore[line-too-long]
"""Submission packs API (Epic 61, spec §A6). Internal principals; API keys may read only (writes are human-only).

| Method + path | Permission |
|---|---|
| ``GET /applications/{id}/submission-pack`` | ``submission:read`` (404 when none) |
| ``POST /applications/{id}/submission-pack/checks`` | ``submission:read`` ("Refresh checks"; nothing is written) |
| ``POST /applications/{id}/submission-pack`` | ``submission:draft`` (manual pack without the agent; works with AI off) |
| ``GET /submission-packs`` · ``GET /submission-packs/agent-status`` · ``GET /submission-packs/metrics`` · ``GET /submission-packs/{id}`` | ``submission:read`` (metrics BU+) |
| ``PATCH /submission-packs/{id}`` · ``POST /submission-packs/{id}/opened`` · ``POST /submission-packs/{id}/discard`` | ``submission:create`` |
| ``GET`` / ``PUT /clients/{id}/submission-rules`` | ``client:read`` / ``pipeline:configure`` |

Running the Submission Agent is the foundation's ``POST /agent-runs {agent_key: "submission", subject_type:
"application", subject_id}``.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.errors import NotFound
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.submission_packs import (
    ClientSubmissionRulesIn,
    ClientSubmissionRulesOut,
    PackAgentStatusOut,
    PackChecksIn,
    PackChecksOut,
    PackDetailOut,
    PackDiscardIn,
    PackMetricsOut,
    PackPage,
    PackPatchIn,
)
from app.services import submission_packs_service as svc

router = APIRouter(tags=["submission-packs"])
DB = TenantDBSessionDep
PackRead = Annotated[Principal, Depends(require("submission:read"))]
PackDraft = Annotated[Principal, Depends(require("submission:draft"))]
PackSubmit = Annotated[Principal, Depends(require("submission:create"))]
ClientRead = Annotated[Principal, Depends(require("client:read"))]
Configure = Annotated[Principal, Depends(require("pipeline:configure"))]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("/applications/{application_id}/submission-pack", response_model=PackDetailOut)
async def get_application_pack(application_id: uuid.UUID, _: PackRead, db: DB) -> PackDetailOut:
    pack = await svc.get_open_pack(get_context(), db, application_id)
    if pack is None:
        raise NotFound("No submission pack yet")
    return pack


@router.post("/applications/{application_id}/submission-pack/checks", response_model=PackChecksOut)
async def refresh_checks(application_id: uuid.UUID, body: PackChecksIn, _: PackRead, db: DB) -> PackChecksOut:
    checks = await svc.run_checks(get_context(), db, application_id, rates=body.rates)
    return PackChecksOut(checks=list(checks))


@router.post(
    "/applications/{application_id}/submission-pack",
    response_model=PackDetailOut,
    status_code=status.HTTP_201_CREATED,
)
async def prepare_manual(application_id: uuid.UUID, _: PackDraft, db: DB) -> PackDetailOut:
    _human()
    ctx = get_context()
    pack = await svc.prepare(ctx, db, application_id)
    return await svc.get_detail(ctx, db, pack.id)


@router.get("/submission-packs", response_model=PackPage)
async def list_packs(
    _: PackRead,
    db: DB,
    client_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status", pattern="^(draft|ready|blocked)$")] = None,
    lifecycle: Annotated[str | None, Query(pattern="^(open|used|discarded|stale)$")] = None,
    owner: uuid.UUID | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> PackPage:
    return await svc.list_packs(
        get_context(), db, client_id=client_id, status=status_, lifecycle=lifecycle, owner_id=owner,
        date_from=date_from, date_to=date_to, page=page, page_size=page_size,
    )  # fmt: skip


@router.get("/submission-packs/agent-status", response_model=PackAgentStatusOut)
async def agent_status(_: PackRead, db: DB) -> PackAgentStatusOut:
    return PackAgentStatusOut(**await svc.agent_status(get_context(), db))


@router.get("/submission-packs/metrics", response_model=PackMetricsOut)
async def pack_metrics(
    _: PackRead,
    db: DB,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> PackMetricsOut:
    return await svc.metrics(get_context(), db, date_from=date_from, date_to=date_to)


@router.get("/submission-packs/{pack_id}", response_model=PackDetailOut)
async def get_pack(pack_id: uuid.UUID, _: PackRead, db: DB) -> PackDetailOut:
    return await svc.get_detail(get_context(), db, pack_id)


@router.patch("/submission-packs/{pack_id}", response_model=PackDetailOut)
async def patch_pack(pack_id: uuid.UUID, body: PackPatchIn, _: PackSubmit, db: DB) -> PackDetailOut:
    _human()
    return await svc.patch(get_context(), db, pack_id, body)


@router.post("/submission-packs/{pack_id}/opened", response_model=PackDetailOut)
async def opened(pack_id: uuid.UUID, _: PackSubmit, db: DB) -> PackDetailOut:
    _human()
    return await svc.mark_opened(get_context(), db, pack_id)


@router.post("/submission-packs/{pack_id}/discard", response_model=PackDetailOut)
async def discard(pack_id: uuid.UUID, body: PackDiscardIn, _: PackSubmit, db: DB) -> PackDetailOut:
    _human()
    return await svc.discard(get_context(), db, pack_id, body.reason)


@router.get("/clients/{client_id}/submission-rules", response_model=ClientSubmissionRulesOut)
async def get_rules(client_id: uuid.UUID, _: ClientRead, db: DB) -> ClientSubmissionRulesOut:
    return await svc.get_client_rules(get_context(), db, client_id)


@router.put("/clients/{client_id}/submission-rules", response_model=ClientSubmissionRulesOut)
async def put_rules(
    client_id: uuid.UUID, body: ClientSubmissionRulesIn, _: Configure, db: DB
) -> ClientSubmissionRulesOut:
    _human()
    return await svc.put_client_rules(get_context(), db, client_id, body)
