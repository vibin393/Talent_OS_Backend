# ruff: file-ignore[line-too-long]
"""Secure Link assessment page (Epic 40 A6): ``/api/v1/public/links/{token}/assessment`` (purpose
``assessment_attempt``; actor ``secure_link``).

No session cookie: the token is the credential and is never logged (the log redactor masks ``/l/<token>`` and the
links API). Rate-limited per IP. Responses are the candidate-facing delivery models (no keys). Viewing never
consumes the multi-use link; the attempt session id travels in ``X-Attempt-Session``.
"""

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Response

from app.core import rate_limit
from app.core.context import get_context
from app.schemas.assessments_delivery import (
    AttemptView,
    PreStartOut,
    SaveIn,
    SaveOut,
    SignalsAcceptedOut,
    SignalsIn,
    StartIn,
    SubmitOut,
)
from app.services.assessments import taking

router = APIRouter(prefix="/public/links", tags=["public"])
Token = Annotated[str, Path(min_length=20, max_length=200)]
Session = Annotated[str | None, Header(alias="X-Attempt-Session", max_length=200)]
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}
# Autosave (15 s) + signals (10 s) for a 3-hour test stay well below this per-IP budget.
IP_LIMIT_PER_HOUR = 3000


async def _guard(response: Response, token: Token) -> str:
    response.headers.update(PUBLIC_HEADERS)
    ip = get_context().ip or "unknown"
    await rate_limit.enforce(
        "assessment_link_ip", hashlib.sha256(ip.encode()).hexdigest()[:32], IP_LIMIT_PER_HOUR, 3600
    )
    return token


PublicToken = Annotated[str, Depends(_guard)]


async def _subject(token: str) -> taking.Subject:
    return await taking.link_subject(token, get_context().ip)


@router.get("/{token}/assessment", response_model=PreStartOut)
async def prestart(token: PublicToken) -> Any:
    return await taking.prestart(await _subject(token))


@router.post("/{token}/assessment/start", response_model=AttemptView)
async def start(token: PublicToken, body: StartIn, session: Session = None) -> Any:
    return await taking.start(await _subject(token), body, session)


@router.get("/{token}/assessment/attempt", response_model=AttemptView)
async def attempt(token: PublicToken, session: Session = None) -> Any:
    return await taking.attempt(await _subject(token), session)


@router.put("/{token}/assessment/attempt/answers", response_model=SaveOut)
async def save(token: PublicToken, body: SaveIn, session: Session = None) -> Any:
    return await taking.save(await _subject(token), session, body)


@router.post("/{token}/assessment/attempt/sections/{key}/finish", response_model=AttemptView)
async def finish_section(token: PublicToken, key: str, session: Session = None) -> Any:
    return await taking.finish_section(await _subject(token), session, key)


@router.post("/{token}/assessment/attempt/signals", response_model=SignalsAcceptedOut)
async def signals(token: PublicToken, body: SignalsIn, session: Session = None) -> Any:
    return await taking.signals(await _subject(token), session, body)


@router.post("/{token}/assessment/attempt/submit", response_model=SubmitOut)
async def submit(token: PublicToken, session: Session = None) -> Any:
    return await taking.submit(await _subject(token), session)
