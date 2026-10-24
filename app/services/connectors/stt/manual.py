"""Manual ``stt`` adapter (Epic 66, spec 0.9): ``transcribe`` raises ``ManualActionRequired`` -- upload a transcript
(the Phase 4 Manual path)."""

from collections.abc import Mapping
from typing import Any

from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import ManualActionRequired
from app.services.connectors.stt.port import TranscriptDTO

__all__ = ["INSTRUCTIONS", "ManualSpeechToText"]

INSTRUCTIONS = "Upload a transcript for this recording"


class ManualSpeechToText:
    category = "stt"
    provider_key = "manual"
    key = "manual"

    def __init__(self, ctx: AdapterContext | None = None) -> None:
        self.ctx = ctx

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(f"stt: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def transcribe(self, audio: bytes, media_type: str, language_hint: str | None) -> TranscriptDTO:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(INSTRUCTIONS, {"op": "transcribe", "media_type": media_type})

    async def languages(self) -> tuple[str, ...]:  # NOSONAR
        return ()
