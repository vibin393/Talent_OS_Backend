# ruff: noqa: E501
"""Epic 76 (story 76.3, D-6): the Azure AI Speech adapter against a recorded fast-transcription response (no network),
plus the dual-channel split, chunking and merge helpers of ``telephony.stt_jobs``."""

import io
import json
import struct
import uuid
import wave
from collections.abc import Iterator

import httpx
import pytest

from app.core import net_safety
from app.core.integrations import ConnectorAuthError, MappingSecretBag, make_context
from app.services.connectors.stt.manifests import AZURE_SPEECH
from app.services.connectors.voice.port import TranscriptDTO, TranscriptSegmentDTO
from app.services.telephony import stt_jobs

FIXTURE = {
    "durationMilliseconds": 9000,
    "combinedPhrases": [{"text": "Hello. Haan, main interested hoon."}],
    "phrases": [
        {"channel": 0, "offsetMilliseconds": 500, "durationMilliseconds": 1200, "text": "Hello.", "locale": "en-IN", "confidence": 0.9},
        {"channel": 0, "offsetMilliseconds": 2500, "durationMilliseconds": 3000, "text": "Haan, main interested hoon.", "locale": "hi-IN", "confidence": 0.8},
        {"channel": 0, "offsetMilliseconds": 6000, "durationMilliseconds": 1000, "text": "Thank you very much for calling.", "locale": "en-IN", "confidence": 0.9},
    ],
}


async def _resolve(host: str) -> list[str]:
    if host == "centralindia.api.cognitive.microsoft.com":
        return ["52.1.2.3"]
    raise OSError("NXDOMAIN")


@pytest.fixture(autouse=True)
def _resolver() -> Iterator[None]:
    net_safety.set_resolver(_resolve)
    yield
    net_safety.set_resolver(None)


def _adapter(handler: object, key: str = "k-123") -> object:
    ctx = make_context(
        AZURE_SPEECH, tenant_id=uuid.uuid4(), installation_id=uuid.uuid4(), config_id=uuid.uuid4(), settings={},
        secrets=MappingSecretBag({"subscription_key": key}), mode="live", transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )  # fmt: skip
    return AZURE_SPEECH.adapters["live"](ctx)


async def test_transcribe_maps_phrases_and_sends_both_locales() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=FIXTURE)

    adapter = _adapter(handler)
    dto = await adapter.transcribe(b"RIFFfake", "audio/wav", None)  # type: ignore[attr-defined]
    req = seen[0]
    assert req.headers["host"] == "centralindia.api.cognitive.microsoft.com"  # pinned IP, Host kept
    assert req.url.path == "/speechtotext/transcriptions:transcribe" and req.url.params["api-version"] == "2024-11-15"
    assert req.headers["Ocp-Apim-Subscription-Key"] == "k-123"
    body = req.content.decode("latin-1")
    definition = json.loads(body.split('name="definition"')[1].split("\r\n\r\n", 1)[1].split("\r\n--")[0])
    assert definition["locales"] == ["en-IN", "hi-IN"] and "diarization" not in definition
    assert [s.text for s in dto.segments] == ["Hello.", "Haan, main interested hoon.", "Thank you very much for calling."]
    assert (dto.segments[1].start_ms, dto.segments[1].end_ms, dto.segments[0].speaker) == (2500, 5500, "unknown")
    assert dto.language == "en-IN"
    assert await adapter.languages() == ("en-IN", "hi-IN")  # type: ignore[attr-defined]


async def test_auth_and_server_errors_are_classified() -> None:
    from app.core.integrations import ConnectorTransientError

    with pytest.raises(ConnectorAuthError):
        await _adapter(lambda r: httpx.Response(401)).transcribe(b"x", "audio/wav", None)  # type: ignore[attr-defined]
    with pytest.raises(ConnectorTransientError):
        await _adapter(lambda r: httpx.Response(503)).transcribe(b"x", "audio/wav", "hi-IN")  # type: ignore[attr-defined]


def _stereo(left: list[int], right: list[int], rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<hh", a, b) for a, b in zip(left, right, strict=True)))
    return buf.getvalue()


def test_split_channels_deinterleaves_pcm16() -> None:
    data = _stereo([1, 2, 3, -4], [100, 200, -300, 400])
    rate, ch1, ch2 = stt_jobs.split_channels(data)  # type: ignore[misc]
    assert rate == 8000
    assert struct.unpack("<4h", ch1) == (1, 2, 3, -4)
    assert struct.unpack("<4h", ch2) == (100, 200, -300, 400)
    assert stt_jobs.split_channels(stt_jobs.mono_wav(b"\x00\x00" * 4, 8000)) is None
    assert stt_jobs.split_channels(b"not a wav") is None


def test_chunks_on_frame_boundaries_with_offsets() -> None:
    pcm = b"\x01\x00" * (8000 * 7)  # 7 s mono
    parts = stt_jobs.chunks(pcm, 8000, seconds=3)
    assert [o for o, _ in parts] == [0, 3000, 6000]
    assert all(len(p) % 2 == 0 for _, p in parts) and sum(len(p) for _, p in parts) == len(pcm)


def test_merge_labels_channels_and_orders_by_start() -> None:
    a = TranscriptDTO((TranscriptSegmentDTO("unknown", 0, 900, "Hello"), TranscriptSegmentDTO("unknown", 4000, 5000, "Notice period?")), "en-IN")
    b = TranscriptDTO((TranscriptSegmentDTO("unknown", 1000, 2000, "Hi"),), "en-IN")
    c = TranscriptDTO((TranscriptSegmentDTO("unknown", 0, 500, "Sixty days"),), "en-IN")
    segs, lang = stt_jobs.merge([("recruiter", a, 0), ("candidate", b, 0), ("candidate", c, 300_000)])
    assert [(s.speaker, s.start_ms, s.text) for s in segs] == [
        ("recruiter", 0, "Hello"), ("candidate", 1000, "Hi"), ("recruiter", 4000, "Notice period?"), ("candidate", 300_000, "Sixty days"),
    ]  # fmt: skip
    assert lang == "en-IN"


async def test_transcribe_audio_dual_channel_uses_one_pass_per_channel() -> None:
    from app.core.integrations import make_context as mk
    from app.services.connectors.stt.fake import FakeSpeechToText

    FakeSpeechToText.reset()
    fake = FakeSpeechToText(mk(AZURE_SPEECH, tenant_id=uuid.uuid4(), installation_id=None, config_id=None, settings={}, secrets=MappingSecretBag({}), mode="fake"))
    segs, _ = await stt_jobs.transcribe_audio(fake, _stereo([0] * 800, [0] * 800), "audio/wav", ["en-IN"])
    assert [s.speaker for s in segs] == ["recruiter", "candidate"]
    assert len(FakeSpeechToText.calls) == 2
    mono, _ = await stt_jobs.transcribe_audio(fake, b"ID3notwav", "audio/mpeg", [])
    assert [s.speaker for s in mono] == ["unknown"]
