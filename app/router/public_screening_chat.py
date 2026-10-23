# ruff: file-ignore[line-too-long]
"""Secure-Link screening chat (Epic 57 A7): ``/api/v1/public/screening-chat/{token}`` (purpose ``agent_conversation``).

No session cookie: the token is the credential and is never logged. Rate-limited per IP and (messages) per link.
Viewing never consumes the multi-use link. Responses are the candidate-facing projection only.
"""

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response

from app.core import rate_limit
from app.core.context import get_context
from app.schemas.screening_chat_portal import ChatConsentIn, ChatMessageIn, PortalScreeningChat
from app.services import screening_agent_service as svc

router = APIRouter(prefix="/public/screening-chat", tags=["public"])
Token = Annotated[str, Path(min_length=20, max_length=200)]
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}
IP_LIMIT_PER_HOUR = 1200


async def _guard(response: Response, token: Token) -> str:
    response.headers.update(PUBLIC_HEADERS)
    ip = get_context().ip or "unknown"
    await rate_limit.enforce(
        "screening_chat_ip", hashlib.sha256(ip.encode()).hexdigest()[:32], IP_LIMIT_PER_HOUR, 3600
    )
    return token


PublicToken = Annotated[str, Depends(_guard)]


@router.get("/{token}", response_model=PortalScreeningChat)
async def view(token: PublicToken, after_seq: Annotated[int, Query(ge=0)] = 0) -> Any:
    return await svc.portal_chat.view(await svc.portal_chat.link_subject(token), after_seq)


@router.post("/{token}/consent", response_model=PortalScreeningChat)
async def consent(token: PublicToken, body: ChatConsentIn) -> Any:
    return await svc.portal_chat.consent(
        await svc.portal_chat.link_subject(token), body.decision, body.notice_id
    )


@router.post("/{token}/messages", response_model=PortalScreeningChat)
async def message(token: PublicToken, body: ChatMessageIn) -> Any:
    return await svc.portal_chat.message(await svc.portal_chat.link_subject(token), body.body)


@router.post("/{token}/handoff", response_model=PortalScreeningChat)
async def handoff(token: PublicToken) -> Any:
    return await svc.portal_chat.handoff(await svc.portal_chat.link_subject(token))
