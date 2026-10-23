# ruff: file-ignore[line-too-long]
"""Calendar push endpoint (Epic 72, spec A4): ``POST /api/v1/hooks/calendar/{channel_id}``.

Public (no session / CSRF; the global anonymous IP limit applies). Mounted BEFORE ``connector_hooks`` so the generic
``/hooks/{category}/{config_id}`` route never shadows it. Google: channel token + resource id (401 on mismatch);
Microsoft: ``clientState`` + subscription id (forged items dropped, always 202) and the ``validationToken`` handshake
(200 ``text/plain``). Notifications only trigger a delta sync."""

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response

from app.services import calendar_sync_service as svc

router = APIRouter(prefix="/hooks/calendar", tags=["hooks"])


@router.post("/{channel_id}", response_model=None)
async def receive(channel_id: uuid.UUID, request: Request) -> Any:
    body = await request.body()
    result = await svc.handle_push(channel_id, dict(request.headers), dict(request.query_params), body)
    if result.text is not None:
        return Response(content=result.text, media_type="text/plain", status_code=result.status_code)
    return Response(status_code=result.status_code)
