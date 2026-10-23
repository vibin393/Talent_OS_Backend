# ruff: file-ignore[line-too-long]
"""Azure AI Speech adapter (Epic 76, spec A5; key ``azure_speech``; coordinator decision D-6: Central India, ``en-IN``
and ``hi-IN``).

Fast transcription (non-streaming): ``POST https://centralindia.api.cognitive.microsoft.com/speechtotext/
transcriptions:transcribe?api-version=<api_version>`` (multipart ``audio`` + ``definition``) with header
``Ocp-Apim-Subscription-Key``. Both locales are passed for language identification on code-mixed audio; Azure
diarisation is requested only for mono audio when asked (dual-channel recordings are split by Talent OS and each
channel is transcribed separately). The response ``phrases`` (offset / duration ms, text, locale) become
``TranscriptSegmentDTO`` rows with speaker ``unknown`` (the caller labels channels).
"""

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import (
    ConnectorAuthError,
    ConnectorPermanentError,
    ConnectorRateLimited,
    ConnectorTransientError,
    ManualActionRequired,
)
from app.services.connectors.stt.port import TranscriptDTO, TranscriptSegmentDTO

__all__ = ["API_VERSION", "EGRESS_HOSTS", "LANGUAGES", "AzureSpeechConfig", "AzureSpeechToText", "parse_response"]

API_VERSION = "2024-11-15"
LANGUAGES: tuple[str, ...] = ("en-IN", "hi-IN")
EGRESS_HOSTS = ("centralindia.api.cognitive.microsoft.com", "centralindia.stt.speech.microsoft.com")
FAST_MAX_BYTES = 200 * 1024 * 1024  # A0: fast transcription accepts < 300 MB / < 2 h; chunked above (stt_jobs)


class AzureSpeechConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: Literal["centralindia"] = "centralindia"
    api_version: str = Field(default=API_VERSION, max_length=20, pattern=r"^\d{4}-\d{2}-\d{2}(-preview)?$")
    profanity: Literal["masked", "raw"] = "masked"
    stt_rate_per_minute_minor: int = Field(default=0, ge=0, le=100000)


def parse_response(body: Mapping[str, Any], fallback_language: str | None) -> TranscriptDTO:
    phrases = body.get("phrases") or []
    segments: list[TranscriptSegmentDTO] = []
    locales: dict[str, int] = {}
    for p in phrases:
        text = str(p.get("text") or "").strip()
        if not text:
            continue
        start = int(p.get("offsetMilliseconds") or 0)
        end = start + int(p.get("durationMilliseconds") or 0)
        segments.append(TranscriptSegmentDTO("unknown", start, end, text))
        loc = p.get("locale")
        if loc:
            locales[str(loc)] = locales.get(str(loc), 0) + len(text)
    language = max(locales, key=locales.__getitem__) if locales else fallback_language
    return TranscriptDTO(segments=tuple(segments), language=language)


class AzureSpeechToText:
    category = "stt"
    provider_key = "azure_speech"

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.key = self.provider_key

    def __repr__(self) -> str:
        return f"AzureSpeechToText(mode={self.ctx.mode})"

    @property
    def _cfg(self) -> AzureSpeechConfig:
        return self.ctx.config  # type: ignore[return-value]

    def _headers(self) -> dict[str, str]:
        key = self.ctx.secrets.get("subscription_key")
        if not key:
            msg = "The Speech resource key is missing"
            raise ConnectorAuthError(msg)
        return {"Ocp-Apim-Subscription-Key": key}

    def _url(self) -> str:
        return (
            f"https://{self._cfg.region}.api.cognitive.microsoft.com/speechtotext/transcriptions:transcribe"
            f"?api-version={self._cfg.api_version}"
        )

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        if op == "health":
            return {"ok": True, "languages": list(LANGUAGES)}
        raise ManualActionRequired(f"stt: {op} is not an operation of this provider", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def languages(self) -> tuple[str, ...]:
        return LANGUAGES

    async def transcribe(self, audio: bytes, media_type: str, language_hint: str | None) -> TranscriptDTO:
        return await self.transcribe_with(audio, media_type, language_hint)

    async def transcribe_with(
        self, audio: bytes, media_type: str, language_hint: str | None, *, locales: tuple[str, ...] | None = None, diarize: bool = False
    ) -> TranscriptDTO:
        if len(audio) > FAST_MAX_BYTES:
            raise ConnectorPermanentError("audio_too_large", "The audio is too large for one transcription request")
        wanted = tuple(loc for loc in (locales or LANGUAGES) if loc in LANGUAGES) or LANGUAGES
        if language_hint in LANGUAGES:
            wanted = (language_hint, *[w for w in wanted if w != language_hint])  # type: ignore[assignment]
        definition: dict[str, Any] = {"locales": list(wanted), "profanityFilterMode": "Masked" if self._cfg.profanity == "masked" else "None"}
        if diarize:
            definition["diarization"] = {"maxSpeakers": 2, "enabled": True}
        files = {
            "audio": ("audio", audio, media_type or "audio/wav"),
            "definition": (None, json.dumps(definition), "application/json"),
        }
        resp = await self.ctx.http.post(self._url(), headers=self._headers(), files=files)
        code = resp.status_code
        if code in (401, 403):
            msg = "The Speech service rejected the key"
            raise ConnectorAuthError(msg)
        if code == 429:
            retry = resp.headers.get("retry-after", "30")
            raise ConnectorRateLimited(float(retry) if retry.strip().isdigit() else 30.0)
        if code >= 500:
            msg = f"speech service error {code}"
            raise ConnectorTransientError(msg)
        if not 200 <= code < 300:
            raise ConnectorPermanentError(f"stt_{code}", f"The Speech service refused the audio (HTTP {code})")
        return parse_response(resp.json(), wanted[0])
