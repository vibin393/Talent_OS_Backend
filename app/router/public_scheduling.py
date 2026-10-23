# ruff: file-ignore[line-too-long]
"""Secure-Link self-scheduling page (Epic 59 A6): ``/api/v1/public/slot-pick/{token}`` (purpose ``slot_pick``).

No session cookie: the token is the credential and is never logged. Rate-limited per IP. Viewing never consumes the
multi-use link. Responses are the Candidate-facing projection only (no participants, no availability details)."""

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Response

from app.core import rate_limit
from app.core.context import get_context
from app.schemas.scheduling_public import CounterIn, PickIn, PickResultOut, SlotPickView, TimezoneIn
from app.services import scheduling_agent_service as svc

router = APIRouter(prefix="/public/slot-pick", tags=["public"])
Token = Annotated[str, Path(min_length=20, max_length=200)]
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}
IP_LIMIT_PER_HOUR = 600


async def _guard(response: Response, token: Token) -> str:
    response.headers.update(PUBLIC_HEADERS)
    ip = get_context().ip or "unknown"
    await rate_limit.enforce(
        "slot_pick_ip", hashlib.sha256(ip.encode()).hexdigest()[:32], IP_LIMIT_PER_HOUR, 3600
    )
    return token


PublicToken = Annotated[str, Depends(_guard)]


@router.get("/{token}", response_model=SlotPickView)
async def view(token: PublicToken) -> Any:
    return await svc.offers.view(await svc.offers.link_subject(token))


@router.post("/{token}/pick", response_model=PickResultOut)
async def pick(token: PublicToken, body: PickIn) -> Any:
    out, dedup = await svc.offers.pick(await svc.offers.link_subject(token), body.index, body.pick_id)
    return PickResultOut(status=out.status, deduplicated=dedup, view=out)


@router.post("/{token}/counter", response_model=SlotPickView)
async def counter(token: PublicToken, body: CounterIn) -> Any:
    return await svc.offers.counter(await svc.offers.link_subject(token), body.windows, body.note)


@router.post("/{token}/timezone", response_model=SlotPickView)
async def timezone(token: PublicToken, body: TimezoneIn) -> Any:
    return await svc.offers.set_timezone(await svc.offers.link_subject(token), body.tz)
