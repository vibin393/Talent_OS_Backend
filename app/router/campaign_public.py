# ruff: file-ignore[line-too-long]
"""Public marketing links (Epic 39 A6): ``/api/v1/public/m/{token}/*``. No session: the signed token is the credential
(``campaigns.tokens``; a bad / forged token or one of another tenant is always 404, never 401). Rate-limited per IP
(``campaign_public`` 120/min). Responses are ``no-store`` / ``no-referrer`` / ``noindex``; tokens are never logged.

* ``GET o.gif`` -- 1x1 GIF; records ``opened`` once per message, only when the message was rendered with a pixel.
* ``GET c/{index}`` -- records ``clicked`` and 302s to the stored job link (+ ``tos_ct``); unknown index -> 404.
* ``GET u`` / ``POST u`` -- unsubscribe page data / opt-out (JSON ``{"all": bool}`` or the RFC 8058 one-click form
  body ``List-Unsubscribe=One-Click``), applied synchronously.
"""

import base64
import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import RedirectResponse

from app.core import rate_limit
from app.core.context import get_context
from app.core.database import platform_session, tenant_session
from app.core.errors import NotFound
from app.schemas.campaign_public import UnsubscribeIn, UnsubscribeOut, UnsubscribeView
from app.services.campaigns import engagement, tokens

router = APIRouter(prefix="/public/m", tags=["public"])
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
Token = Annotated[str, Path(min_length=20, max_length=120)]


async def _guard(response: Response, token: Token) -> str:
    response.headers.update(PUBLIC_HEADERS)
    ip = get_context().ip or "unknown"
    await rate_limit.enforce("campaign_public", hashlib.sha256(ip.encode()).hexdigest()[:32], 120, 60)
    return token


Guarded = Annotated[str, Depends(_guard)]


def _parse(token: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    parsed = tokens.parse(token)
    if parsed is None:
        raise NotFound("Link not found")
    return parsed


async def _message(db: Any, message_id: uuid.UUID, tenant_id: uuid.UUID, mac: str) -> Any:
    cm = await engagement.message_for_token(db, message_id)
    if (
        cm is None
        or cm.tenant_id != tenant_id
        or not tokens.verify(tenant_id, message_id, mac, cm.token_nonce)
    ):
        raise NotFound("Link not found")
    return cm


async def _tenant_name(tenant_id: uuid.UUID) -> str | None:
    from app.models.identity import Tenant

    async with platform_session() as pdb:
        tenant = await pdb.get(Tenant, tenant_id)
    return str(tenant.name) if tenant is not None else None


@router.get("/{token}/o.gif")
async def open_pixel(token: Guarded) -> Response:
    parsed = tokens.parse(token)
    if parsed is not None and await _tenant_name(parsed[0]) is not None:
        try:
            async with tenant_session(parsed[0]) as db:
                cm = await _message(db, parsed[1], parsed[0], parsed[2])
                await engagement.opened(db, cm)
        except NotFound:
            pass  # the pixel always answers with the image (no oracle)
    return Response(content=GIF, media_type="image/gif", headers=PUBLIC_HEADERS)


@router.get("/{token}/c/{index}")
async def click(token: Guarded, index: Annotated[int, Path(ge=0, le=50)]) -> RedirectResponse:
    tenant_id, message_id, mac = _parse(token)
    if await _tenant_name(tenant_id) is None:
        raise NotFound("Link not found")
    async with tenant_session(tenant_id) as db:
        cm = await _message(db, message_id, tenant_id, mac)
        url = await engagement.clicked(db, cm, index)
    if url is None:
        raise NotFound("Link not found")
    return RedirectResponse(url, status_code=302, headers=PUBLIC_HEADERS)


@router.get("/{token}/u", response_model=UnsubscribeView)
async def unsubscribe_view(token: Guarded) -> UnsubscribeView:
    tenant_id, message_id, mac = _parse(token)
    name = await _tenant_name(tenant_id)
    if name is None:
        raise NotFound("Link not found")
    async with tenant_session(tenant_id) as db:
        cm = await _message(db, message_id, tenant_id, mac)
        already = await engagement.unsubscribe_state(db, cm)
        channel = cm.channel
    return UnsubscribeView(tenant_name=name, channel=channel, already_unsubscribed=already)


@router.post("/{token}/u", response_model=UnsubscribeOut)
async def unsubscribe(token: Guarded, request: Request) -> UnsubscribeOut:
    tenant_id, message_id, mac = _parse(token)
    if await _tenant_name(tenant_id) is None:
        raise NotFound("Link not found")
    all_marketing = False
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            all_marketing = UnsubscribeIn.model_validate(await request.json()).all
        except ValueError:
            all_marketing = False
    async with tenant_session(tenant_id) as db:
        cm = await _message(db, message_id, tenant_id, mac)
        await engagement.unsubscribe(db, cm, all_marketing=all_marketing)
        channel = cm.channel
    return UnsubscribeOut(status="unsubscribed", channel=channel, all=all_marketing)
