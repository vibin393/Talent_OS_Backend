# ruff: file-ignore[line-too-long]
"""Analytics answers (Epic 64, FR-316..FR-318): ``/analytics/answers/*``. Internal users only (API keys refused, 403);
every answer is private to its user (404 for anyone else). Asking goes through the Copilot endpoints (Epic 54,
``agent_key="analytics"``); these endpoints read, re-preview (as the current viewer) and link saved reports."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.analytics_answers import AnswerOut, AnswerSavedIn, AnswerSummaryOut
from app.schemas.report_builder import RunResultOut
from app.services import analytics_answers_service as svc

router = APIRouter(tags=["analytics-answers"])
DB = TenantDBSessionDep
Paging = Annotated[PageParams, Depends()]
Read = Annotated[Principal, Depends(require("report:read"))]
Build = Annotated[Principal, Depends(require("report:build"))]


@router.get("/analytics/answers", response_model=Page[AnswerSummaryOut])
async def list_answers(
    _: Read, db: DB, params: Paging, conversation_id: Annotated[uuid.UUID | None, Query()] = None
) -> Any:
    return await svc.list_answers(get_context(), db, params, conversation_id=conversation_id)


@router.get("/analytics/answers/{answer_id}", response_model=AnswerOut)
async def get_answer(answer_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.get_answer(get_context(), db, answer_id)


@router.post("/analytics/answers/{answer_id}/preview", response_model=RunResultOut)
async def preview_answer(answer_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.preview_answer(get_context(), db, answer_id)


@router.post("/analytics/answers/{answer_id}/saved", response_model=AnswerOut)
async def mark_saved(answer_id: uuid.UUID, body: AnswerSavedIn, _: Build, db: DB) -> Any:
    return await svc.mark_saved(get_context(), db, answer_id, body.report_id)
