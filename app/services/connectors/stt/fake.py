"""Fake ``stt`` adapter (Epic 66, spec 0.9): returns the scripted transcript (``script["transcript"]`` = list of
``[speaker, text]``) or a deterministic one derived from the audio length (8 kHz 16-bit mono). No network."""

from app.core.integrations.fakes import FakeAdapterBase
from app.services.connectors.stt.port import TranscriptDTO, TranscriptSegmentDTO

__all__ = ["LANGUAGES", "FakeSpeechToText"]

LANGUAGES: tuple[str, ...] = ("en", "hi", "ta", "te", "kn", "mr", "bn")
_SPEAKERS = ("agent", "candidate", "interviewer", "recruiter", "unknown")


class FakeSpeechToText(FakeAdapterBase):
    category = "stt"
    provider_key = "fake_stt"

    async def transcribe(self, audio: bytes, media_type: str, language_hint: str | None) -> TranscriptDTO:  # NOSONAR
        self._check("transcribe", (len(audio), media_type, language_hint))
        language = language_hint if language_hint in LANGUAGES else "en"
        scripted = self.scripted().get("transcript")
        if scripted:
            segments = tuple(
                TranscriptSegmentDTO(
                    speaker=speaker if speaker in _SPEAKERS else "unknown",
                    start_ms=None,
                    end_ms=None,
                    text=str(text),
                )
                for speaker, text in scripted
            )
            return TranscriptDTO(segments=segments, language=language)
        duration_ms = len(audio) // 16
        return TranscriptDTO(
            segments=(TranscriptSegmentDTO("unknown", 0, duration_ms, f"Fake transcript of {duration_ms} ms of audio"),),
            language=language,
        )

    async def languages(self) -> tuple[str, ...]:  # NOSONAR
        self._check("languages")
        return LANGUAGES
