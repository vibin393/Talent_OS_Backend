# ruff: file-ignore[line-too-long, missing-trailing-period, missing-terminal-punctuation]
"""Secure-Link pages of the Interview Assistant (Epic 60, A8):

* ``/api/v1/public/interview-recording-consent/{token}`` (purpose ``interview_recording_consent``): the Candidate
  confirms where they are and agrees (or not) to the interview being recorded or transcribed;
* ``/api/v1/public/interview-capture-ack/{token}`` (purpose ``interview_capture_ack``): an external (Client)
  interviewer agrees to recording for this interview.

No session cookie: the token is the credential and is never logged. Rate-limited per IP. Viewing never consumes.
"""

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Response

from app.core import rate_limit
from app.core.context import get_context
from app.schemas.interview_capture_public import (
    CaptureAckIn,
    CaptureAckView,
    RecordingConsentIn,
    RecordingConsentView,
)
from app.services import interview_captures_service as svc

router = APIRouter(prefix="/public", tags=["public"])
Token = Annotated[str, Path(min_length=20, max_length=200)]
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}
IP_LIMIT_PER_HOUR = 600


async def _guard(response: Response, token: Token) -> str:
    response.headers.update(PUBLIC_HEADERS)
    ip = get_context().ip or "unknown"
    await rate_limit.enforce(
        "interview_consent_ip", hashlib.sha256(ip.encode()).hexdigest()[:32], IP_LIMIT_PER_HOUR, 3600
    )
    return token


PublicToken = Annotated[str, Depends(_guard)]


@router.get("/interview-recording-consent/{token}", response_model=RecordingConsentView)
async def consent_view(token: PublicToken) -> Any:
    return await svc.candidate_consent_view(token)


@router.post("/interview-recording-consent/{token}", response_model=RecordingConsentView)
async def consent_decide(token: PublicToken, body: RecordingConsentIn) -> Any:
    return await svc.candidate_consent_decide(token, body.decision, body.country, body.region)


@router.get("/interview-capture-ack/{token}", response_model=CaptureAckView)
async def ack_view(token: PublicToken) -> Any:
    return await svc.external_ack_view(token)


@router.post("/interview-capture-ack/{token}", response_model=CaptureAckView)
async def ack(token: PublicToken, body: CaptureAckIn) -> Any:  # ruff: ignore[unused-function-argument] - agree is always True
    return await svc.external_ack(token)
