# ruff: file-ignore[line-too-long]
"""Route builder shared by the three portal Copilot routers (Epic 77 A5). Not a router module itself.

``build(kind, prefix, principal_dep, use_dep, guard)`` returns an ``APIRouter`` with ``GET /status``,
``GET|POST /conversations``, ``GET|DELETE /conversations/{id}``, ``POST /conversations/{id}/close``,
``POST /conversations/{id}/messages`` (``Idempotency-Key``: a replay returns the first response), ``GET
/turns/{run_id}/events`` (SSE, ``Last-Event-ID`` / ``?after=``), ``POST /turns/{run_id}/cancel|feedback``,
``POST /drafts/{id}/confirm|discard``. Portal sessions only (API keys and Apps never authenticate here); CSRF is
enforced by the portal session dependencies on mutations; responses are FR-80 guarded."""

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.context import get_context
from app.core.pagination import Page
from app.schemas.portal_copilot import (
    DraftConfirmIn,
    DraftResultOut,
    PortalConversationDetailOut,
    PortalConversationOut,
    PortalCopilotStatusOut,
    PortalFeedbackIn,
    PortalMessageIn,
    PortalTurnOut,
)
from app.services import portal_copilot_service as svc
from app.services.copilot.streaming import SSE_HEADERS, redis_client

IDEMPOTENCY_TTL = 86_400


def _valid_id(value: str | None) -> bool:
    if not value:
        return False
    left, _, right = value.partition("-")
    return left.isdigit() and right.isdigit()


async def _replay(key: str) -> dict[str, Any] | None:
    client = redis_client()
    try:
        raw = await client.get(key)
    except Exception:
        return None
    finally:
        await client.aclose()
    return json.loads(raw) if raw else None


async def _remember(key: str, body: dict[str, Any]) -> None:
    client = redis_client()
    try:
        await client.set(key, json.dumps(body, default=str), ex=IDEMPOTENCY_TTL)
    except Exception:
        return
    finally:
        await client.aclose()


def build(kind: str, prefix: str, principal_dep: Any, use_dep: Any, route_class: Any) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[f"{kind}-portal-copilot"], route_class=route_class)
    Who = Annotated[Any, Depends(principal_dep)]  # ruff: ignore[non-lowercase-variable-in-function]
    User = Annotated[Any, Depends(use_dep)] if use_dep is not None else Who  # ruff: ignore[non-lowercase-variable-in-function]

    @router.get("/status", response_model=PortalCopilotStatusOut)
    async def copilot_status(principal: Who) -> Any:
        return await svc.status(get_context(), kind, principal)

    @router.get("/conversations", response_model=Page[PortalConversationOut])
    async def list_conversations(
        principal: User,  # type: ignore[valid-type]
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    ) -> Any:
        return await svc.list_conversations(get_context(), kind, principal, page=page, page_size=page_size)

    @router.post("/conversations", response_model=PortalConversationOut, status_code=status.HTTP_201_CREATED)
    async def create_conversation(principal: User) -> Any:  # type: ignore[valid-type]
        return await svc.create_conversation(get_context(), kind, principal)

    @router.get("/conversations/{conversation_id}", response_model=PortalConversationDetailOut)
    async def get_conversation(conversation_id: uuid.UUID, principal: User) -> Any:  # type: ignore[valid-type]
        return await svc.get_conversation(get_context(), kind, principal, conversation_id)

    @router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_conversation(conversation_id: uuid.UUID, principal: User) -> Response:  # type: ignore[valid-type]
        await svc.delete(get_context(), kind, principal, conversation_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/conversations/{conversation_id}/close", response_model=PortalConversationOut)
    async def close_conversation(conversation_id: uuid.UUID, principal: User) -> Any:  # type: ignore[valid-type]
        return await svc.close(get_context(), kind, principal, conversation_id)

    @router.post("/conversations/{conversation_id}/messages", response_model=PortalTurnOut)
    async def post_message(
        conversation_id: uuid.UUID,
        body: PortalMessageIn,
        principal: User,  # type: ignore[valid-type]
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=100)] = None,
    ) -> Any:
        key = None
        if idempotency_key:
            key = f"pcopilot:idem:{principal.tenant_id}:{kind}:{principal.id}:{conversation_id}:{idempotency_key}"  # type: ignore[attr-defined]
            found = await _replay(key)
            if found is not None:
                return JSONResponse(found)
        out = await svc.post_message(get_context(), kind, principal, conversation_id, body.text)
        if key:
            await _remember(key, out.model_dump(mode="json"))
        return out

    @router.get("/turns/{run_id}/events", response_class=StreamingResponse)
    async def turn_events(
        run_id: uuid.UUID,
        principal: User,  # type: ignore[valid-type]
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=40)] = None,
        after: Annotated[str | None, Query(max_length=40, pattern=r"^\d+-\d+$")] = None,
    ) -> StreamingResponse:
        resume = last_event_id if _valid_id(last_event_id) else after
        events = await svc.stream(get_context(), kind, principal, run_id, after=resume)
        return StreamingResponse(events, media_type="text/event-stream", headers=dict(SSE_HEADERS))

    @router.post("/turns/{run_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
    async def cancel_turn(run_id: uuid.UUID, principal: User) -> Response:  # type: ignore[valid-type]
        await svc.cancel(get_context(), kind, principal, run_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/turns/{run_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
    async def rate_turn(run_id: uuid.UUID, body: PortalFeedbackIn, principal: User) -> Response:  # type: ignore[valid-type]
        await svc.feedback(get_context(), kind, principal, run_id, body)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/drafts/{proposal_id}/confirm", response_model=DraftResultOut)
    async def confirm_draft(
        proposal_id: uuid.UUID, principal: User, body: DraftConfirmIn | None = None  # type: ignore[valid-type]
    ) -> Any:
        return await svc.confirm_draft(
            get_context(), kind, principal, proposal_id, edits=body.edits if body else None
        )

    @router.post("/drafts/{proposal_id}/discard", response_model=DraftResultOut)
    async def discard_draft(proposal_id: uuid.UUID, principal: User) -> Any:  # type: ignore[valid-type]
        return await svc.discard_draft(get_context(), kind, principal, proposal_id)

    return router


__all__ = ["build"]
