"""Public provider webhooks (Epic 13, AD-29): ``POST /api/v1/hooks/{category}/{config_id}``.

No session or CSRF: the tenant is resolved through ``connector_hook_lookups`` and the configured adapter verifies
the signature (401 otherwise); unknown configs get 404; accepted payloads are stored and processed by
``connectors.process_inbound`` (202)."""

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response, status

from app.schemas.connectors import AnyCategory
from app.services import connectors_service as svc

router = APIRouter(prefix="/hooks", tags=["hooks"])


@router.post("/{category}/{config_id}", status_code=status.HTTP_202_ACCEPTED, response_model=dict[str, str])
async def receive(category: AnyCategory, config_id: uuid.UUID, request: Request, response: Response) -> Any:
    body = await request.body()
    ack = await svc.ingest_webhook(
        category, config_id, dict(request.headers), body, client_ip=request.client.host if request.client else None
    )
    if ack.status == "duplicate":  # Epic 66: a replayed provider event id is acknowledged and dropped
        response.status_code = status.HTTP_200_OK
    return {"status": ack.status, "event_id": str(ack.event_id)}
