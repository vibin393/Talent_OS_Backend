"""Manual ``voice`` connector (AD-29, spec 0.13): calls happen outside Talent OS. ``schedule_call`` /
``start_call`` raise ``ManualActionRequired``; everything else returns ``None`` or no-ops. No
``TranscriptionProvider``."""

from collections.abc import Mapping
from typing import Any

from app.services.connectors.base import ConnectorConfig, ManualActionRequired
from app.services.connectors.registry import register_adapter
from app.services.connectors.voice.port import (
    CallSessionDTO,
    CallSessionRequestDTO,
    RecordingDTO,
    TranscriptDTO,
)

INSTRUCTIONS = "Call outside Talent OS, then upload the recording and/or transcript"


class ManualVoiceProvider:
    category = "voice"
    provider_key = "manual"
    key = "manual"

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # noqa: ARG002
        raise ManualActionRequired(f"voice: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # noqa: ARG002
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # noqa: ARG002
        return {}

    async def schedule_call(self, req: CallSessionRequestDTO) -> CallSessionDTO:
        raise ManualActionRequired(
            INSTRUCTIONS, {"op": "schedule_call", "voice_session_id": str(req.voice_session_id)}
        )

    async def start_call(self, external_ref: str) -> CallSessionDTO:
        raise ManualActionRequired(INSTRUCTIONS, {"op": "start_call", "external_ref": external_ref})

    async def end_call(self, external_ref: str) -> CallSessionDTO:
        return CallSessionDTO(external_ref=external_ref, status="manual")

    async def fetch_recording(self, recording_ref: str) -> RecordingDTO | None:  # noqa: ARG002
        return None

    async def fetch_transcript(self, transcript_ref: str) -> TranscriptDTO | None:  # noqa: ARG002
        return None


register_adapter("voice", "manual", ManualVoiceProvider)

__all__ = ["INSTRUCTIONS", "ManualVoiceProvider"]
