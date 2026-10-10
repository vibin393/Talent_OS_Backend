"""``stt`` (speech-to-text) port (Epic 66 contract, spec 0.9; Epic 76 fills real adapters). Same ``transcribe`` shape as
``voice.port.TranscriptionProvider`` so voice code can use either. Every adapter also implements ``ConnectorPort``.
"""

from typing import Protocol, runtime_checkable

from app.services.connectors.base import ConnectorPort
from app.services.connectors.voice.port import TranscriptDTO, TranscriptSegmentDTO

__all__ = ["SpeechToTextPort", "TranscriptDTO", "TranscriptSegmentDTO"]


@runtime_checkable
class SpeechToTextPort(ConnectorPort, Protocol):
    async def transcribe(self, audio: bytes, media_type: str, language_hint: str | None) -> TranscriptDTO: ...

    async def languages(self) -> tuple[str, ...]: ...
