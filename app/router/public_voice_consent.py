# ruff: file-ignore[line-too-long]
"""Secure-Link consent page of Voice Screening (Epic 58, story 58.3; purpose ``voice_consent``):
``/api/v1/public/voice-consent/{token}``. The Candidate confirms where they are and agrees (or not) to the screening call
and, separately, to its recording.

No session cookie: the token is the credential and is never logged. Rate-limited per IP. Viewing never consumes.
"""

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Response

from app.core import rate_limit
from app.core.context import get_context
from app.schemas.voice_public import VoiceConsentIn, VoiceConsentView
from app.services import voice_service as svc

router = APIRouter(prefix="/public", tags=["public"])
Token = Annotated[str, Path(min_length=20, max_length=200)]
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}
IP_LIMIT_PER_HOUR = 600


async def _guard(response: Response, token: Token) -> str:
    response.headers.update(PUBLIC_HEADERS)
    ip = get_context().ip or "unknown"
    await rate_limit.enforce(
        "voice_consent_ip", hashlib.sha256(ip.encode()).hexdigest()[:32], IP_LIMIT_PER_HOUR, 3600
    )
    return token


PublicToken = Annotated[str, Depends(_guard)]


@router.get("/voice-consent/{token}", response_model=VoiceConsentView)
async def view(token: PublicToken) -> Any:
    return await svc.consent_view(token)


@router.post("/voice-consent/{token}", response_model=VoiceConsentView)
async def decide(token: PublicToken, body: VoiceConsentIn) -> Any:
    return await svc.consent_decide(
        token, body.country, body.region, body.voice_screening, body.call_recording
    )
