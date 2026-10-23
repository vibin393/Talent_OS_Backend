# ruff: file-ignore[line-too-long]
"""The sandbox Echo receiver (Epic 68, story 68.5): ``POST /api/v1/webhook-echo/{token}``. No authentication; the
token (``<tenant hex>.<random>``) selects a Sandbox Tenant and is matched by its hash (unknown -> 404)."""

from fastapi import APIRouter, Request, Response

from app.services import webhooks_service

router = APIRouter(prefix="/webhook-echo", tags=["webhooks"])


@router.post("/{token}")
async def receive(token: str, request: Request) -> Response:
    body = (await request.body())[:65_536]
    code, content = await webhooks_service.receive_echo(token[:200], dict(request.headers), body)
    return Response(content=content, status_code=code, media_type="application/json")
