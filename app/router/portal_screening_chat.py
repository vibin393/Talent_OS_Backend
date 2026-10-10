# ruff: noqa: E501
"""Candidate Portal screening chats (Epic 57 A7): ``/api/v1/portal/screening-chats``.

``Depends(current_candidate)`` authenticates the ``tos_candidate`` cookie (+ CSRF on mutations). Every endpoint is scoped
to the signed-in Candidate's own portal chats (another Candidate's chat -> 404). Messages: 20 per minute. Responses are
the candidate-facing projection only (no confidence, flags, review state or run data).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from app.dependencies.candidate import current_candidate
from app.schemas.screening_chat_portal import (
    ChatConsentIn,
    ChatMessageIn,
    PortalScreeningChat,
    PortalScreeningChatSummary,
)
from app.services import screening_agent_service as svc

router = APIRouter(prefix="/portal/screening-chats", tags=["portal"])
Candidate = Annotated[Any, Depends(current_candidate)]
NO_STORE = {"Cache-Control": "no-store"}


def _subject(principal: Any, conversation_id: uuid.UUID, response: Response) -> Any:
    response.headers.update(NO_STORE)
    candidate_id = getattr(principal, "candidate_id", None) or principal.id
    return svc.portal_chat.portal_subject(principal.tenant_id, candidate_id, conversation_id)


@router.get("", response_model=list[PortalScreeningChatSummary])
async def list_mine(principal: Candidate, response: Response) -> Any:
    response.headers.update(NO_STORE)
    candidate_id = getattr(principal, "candidate_id", None) or principal.id
    return await svc.portal_chat.portal_list(principal.tenant_id, candidate_id)


@router.get("/{conversation_id}", response_model=PortalScreeningChat)
async def view(
    conversation_id: uuid.UUID,
    principal: Candidate,
    response: Response,
    after_seq: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    return await svc.portal_chat.view(_subject(principal, conversation_id, response), after_seq)


@router.post("/{conversation_id}/consent", response_model=PortalScreeningChat)
async def consent(
    conversation_id: uuid.UUID, body: ChatConsentIn, principal: Candidate, response: Response
) -> Any:
    return await svc.portal_chat.consent(
        _subject(principal, conversation_id, response), body.decision, body.notice_id
    )


@router.post("/{conversation_id}/messages", response_model=PortalScreeningChat)
async def message(
    conversation_id: uuid.UUID, body: ChatMessageIn, principal: Candidate, response: Response
) -> Any:
    return await svc.portal_chat.message(_subject(principal, conversation_id, response), body.body)


@router.post("/{conversation_id}/handoff", response_model=PortalScreeningChat)
async def handoff(conversation_id: uuid.UUID, principal: Candidate, response: Response) -> Any:
    return await svc.portal_chat.handoff(_subject(principal, conversation_id, response))
