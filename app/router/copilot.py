# ruff: file-ignore[line-too-long]
"""Recruitment Copilot (Epic 54 A6, AD-64): ``/copilot/*`` including the SSE turn stream.

Internal users only; API keys and automated actors are refused (human only). Conversations, messages, turn streams and
feedback are the user's own: anyone else -- administrators included -- gets 404 (coordinator decision 6). Settings
need ``ai:agent_configure`` (reads also ``ai:govern``); admin usage needs ``ai:govern`` and carries no text."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import StreamingResponse

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.pagination import Page
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.copilot import (
    ConversationCreate,
    ConversationOut,
    ConversationQuery,
    ConversationRename,
    ConversationSummaryOut,
    CopilotSettingsIn,
    CopilotSettingsOut,
    CopilotStatusOut,
    CopilotUsageOut,
    FeedbackIn,
    FeedbackOut,
    MessageIn,
    MessageOut,
    TurnOut,
    UsageQuery,
)
from app.services import copilot_service as svc
from app.services.copilot.streaming import SSE_HEADERS

router = APIRouter(prefix="/copilot", tags=["copilot"])
DB = TenantDBSessionDep
Use = Annotated[Principal, Depends(require("copilot:use"))]
Govern = Annotated[Principal, Depends(require("ai:govern"))]
SettingsRead = Annotated[Principal, Depends(require_any("ai:govern", "ai:agent_configure"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]
Idem = Annotated[IdempotencyGuard, Depends(idempotency())]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("/status", response_model=CopilotStatusOut)
async def copilot_status(_: CurrentUserDep, db: DB) -> CopilotStatusOut:
    _human()
    return await svc.status(get_context(), db)


@router.get("/conversations", response_model=Page[ConversationSummaryOut])
async def list_conversations(
    _: Use, db: DB, q: Annotated[ConversationQuery, Query()]
) -> Page[ConversationSummaryOut]:
    _human()
    return await svc.list_conversations(get_context(), db, q)


@router.post("/conversations", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(body: ConversationCreate, _: Use, db: DB) -> ConversationOut:
    _human()
    return await svc.create_conversation(get_context(), db, body)


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
async def get_conversation(conversation_id: uuid.UUID, _: CurrentUserDep, db: DB) -> ConversationOut:
    _human()
    return await svc.get_conversation(get_context(), db, conversation_id)


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def rename_conversation(
    conversation_id: uuid.UUID, body: ConversationRename, _: CurrentUserDep, db: DB
) -> ConversationOut:
    _human()
    return await svc.rename_conversation(get_context(), db, conversation_id, body.title)


@router.post("/conversations/{conversation_id}/close", response_model=ConversationOut)
async def close_conversation(conversation_id: uuid.UUID, _: CurrentUserDep, db: DB) -> ConversationOut:
    _human()
    return await svc.close_conversation(get_context(), db, conversation_id)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Response:
    _human()
    await svc.delete_conversation(get_context(), db, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: uuid.UUID,
    _: CurrentUserDep,
    db: DB,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[MessageOut]:
    _human()
    return await svc.list_messages(get_context(), db, conversation_id, after_seq=after_seq, limit=limit)


@router.post("/conversations/{conversation_id}/messages", response_model=TurnOut)
async def post_message(conversation_id: uuid.UUID, body: MessageIn, _: Use, db: DB, idem: Idem) -> Any:
    _human()
    return await idem.run(lambda: svc.post_message(get_context(), db, conversation_id, body))


@router.get("/turns/{run_id}/events", response_class=StreamingResponse)
async def turn_events(
    run_id: uuid.UUID,
    _: CurrentUserDep,
    db: DB,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=40)] = None,
    after: Annotated[str | None, Query(max_length=40, pattern=r"^\d+-\d+$")] = None,
) -> StreamingResponse:
    """``text/event-stream`` of one turn (run owner only). Resume with ``Last-Event-ID`` (or ``?after=``)."""
    _human()
    resume = last_event_id if last_event_id and _valid_id(last_event_id) else after
    events = await svc.stream_events(get_context(), db, run_id, last_event_id=resume)
    return StreamingResponse(events, media_type="text/event-stream", headers=dict(SSE_HEADERS))


def _valid_id(value: str) -> bool:
    left, _, right = value.partition("-")
    return left.isdigit() and right.isdigit()


@router.post("/turns/{run_id}/cancel", response_model=TurnOut)
async def cancel_turn(run_id: uuid.UUID, _: CurrentUserDep, db: DB) -> TurnOut:
    _human()
    return await svc.cancel_turn(get_context(), db, run_id)


@router.post("/turns/{run_id}/feedback", response_model=FeedbackOut)
async def rate_turn(run_id: uuid.UUID, body: FeedbackIn, _: CurrentUserDep, db: DB) -> FeedbackOut:
    _human()
    return await svc.rate_turn(get_context(), db, run_id, body)


@router.get("/settings", response_model=CopilotSettingsOut)
async def get_settings(_: SettingsRead, db: DB) -> CopilotSettingsOut:
    return await svc.get_settings(get_context(), db)


@router.put("/settings", response_model=CopilotSettingsOut)
async def update_settings(body: CopilotSettingsIn, _: Configure, db: DB) -> CopilotSettingsOut:
    _human()
    return await svc.update_settings(get_context(), db, body)


@router.get("/admin/usage", response_model=CopilotUsageOut)
async def admin_usage(_: Govern, db: DB, q: Annotated[UsageQuery, Query()]) -> CopilotUsageOut:
    return await svc.admin_usage(get_context(), db, q)
