# ruff: noqa: E501
"""Epic 53 (spec 0.13): ``voice`` connector category. The fake satisfies ``VoiceProvider`` + ``ConnectorPort`` and
provides a fake ``TranscriptionProvider``; the manual adapter raises ``ManualActionRequired``; the DB CHECK accepts
``voice``."""

import hashlib
import hmac
import io
import json
import uuid
import wave
from collections.abc import Iterator
from typing import Any

import pytest

from app.core.database import tenant_session
from app.models.connectors import ProviderConnectorConfig
from app.services.connectors import registry
from app.services.connectors.base import ConnectorConfig, ConnectorPort, ManualActionRequired
from app.services.connectors.voice.fake import FakeTranscriptionProvider, FakeVoiceProvider
from app.services.connectors.voice.manual import INSTRUCTIONS
from app.services.connectors.voice.port import CallSessionRequestDTO, TranscriptionProvider, VoiceProvider
from tests.factories import create_tenant_row

SECRET = "whsec-voice"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    FakeVoiceProvider.reset()
    FakeTranscriptionProvider.reset()
    yield
    FakeVoiceProvider.reset()
    FakeTranscriptionProvider.reset()


def _adapter(provider_key: str) -> Any:
    factory = registry.adapter_factory("voice", provider_key)
    assert factory is not None
    return factory(
        ConnectorConfig(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            category="voice",
            provider_key=provider_key,
            legal_entity_id=None,
            settings={},
            credentials=None,
            webhook_secret=SECRET,
        )
    )


def _request(record: bool = True) -> CallSessionRequestDTO:
    return CallSessionRequestDTO(
        tenant_id=uuid.uuid4(),
        voice_session_id=uuid.uuid4(),
        to_e164="+919800000000",
        scheduled_at=None,
        record=record,
        language="en",
        max_duration_s=900,
        callback_url=None,
        script_ref="default",
    )


def test_adapters_registered_and_implement_both_ports() -> None:
    assert {"fake", "manual"} <= set(registry.provider_keys("voice"))
    for key in ("fake", "manual"):
        adapter = _adapter(key)
        assert isinstance(adapter, ConnectorPort)
        assert isinstance(adapter, VoiceProvider)
        assert (adapter.category, adapter.provider_key) == ("voice", key)
    assert isinstance(FakeTranscriptionProvider(), TranscriptionProvider)
    assert not isinstance(_adapter("manual"), TranscriptionProvider)


async def test_fake_scripted_lifecycle() -> None:
    fake: VoiceProvider = _adapter("fake")
    req = _request()
    scheduled = await fake.schedule_call(req)
    assert scheduled.status == "scheduled" and scheduled.external_ref == f"fake-call:{req.voice_session_id}"
    assert (await fake.start_call(scheduled.external_ref)).status == "started"
    ended = await fake.end_call(scheduled.external_ref)
    assert ended.status == "ended" and ended.recording_ref and ended.transcript_ref
    rec = await fake.fetch_recording(ended.recording_ref)
    assert rec is not None and rec.media_type == "audio/wav" and rec.duration_s == 1
    with wave.open(io.BytesIO(rec.data)) as w:
        assert w.getnframes() == w.getframerate() and set(w.readframes(w.getnframes())) == {0}
    tx = await fake.fetch_transcript(ended.transcript_ref)
    assert (
        tx is not None
        and tx.language == "en"
        and [s.speaker for s in tx.segments][:2] == ["agent", "candidate"]
    )
    assert tx == await fake.fetch_transcript(ended.transcript_ref)  # deterministic
    assert await fake.fetch_recording("other") is None and await fake.fetch_transcript("other") is None
    assert [op for op, _ in FakeVoiceProvider.calls][:3] == ["schedule_call", "start_call", "end_call"]
    events = _adapter("fake").lifecycle(scheduled.external_ref)
    assert [e.kind for e in events] == [
        "scheduled",
        "started",
        "ended",
        "recording_ready",
        "transcript_ready",
    ]
    assert events == _adapter("fake").lifecycle(scheduled.external_ref)
    no_rec = await fake.end_call((await fake.schedule_call(_request(record=False))).external_ref)
    assert no_rec.recording_ref is None


async def test_fake_generic_port_and_webhooks() -> None:
    fake = _adapter("fake")
    assert (await fake.handle("sync", {"a": 1}))["ok"] is True
    FakeVoiceProvider.fail_ops.add("start_call")
    with pytest.raises(RuntimeError):
        await fake.start_call("x")
    body = json.dumps({"external_ref": "fake-call:1", "kind": "ended"}).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert fake.verify_webhook({"X-Fake-Signature": sig}, body) is True
    assert fake.verify_webhook({"X-Fake-Signature": "bad"}, body) is False
    assert fake.parse_webhook({}, body) == {"external_ref": "fake-call:1", "kind": "ended"}


async def test_fake_transcription_provider() -> None:
    tx = await FakeTranscriptionProvider().transcribe(b"RIFF....", "audio/wav", "hi")
    assert tx.language == "hi" and tx.segments
    assert FakeTranscriptionProvider.calls == [("transcribe", (8, "audio/wav", "hi"))]


async def test_manual_raises_and_noops() -> None:
    manual = _adapter("manual")
    with pytest.raises(ManualActionRequired, match="Call outside Talent OS") as exc:
        await manual.schedule_call(_request())
    assert exc.value.instructions == INSTRUCTIONS
    with pytest.raises(ManualActionRequired):
        await manual.start_call("x")
    with pytest.raises(ManualActionRequired):
        await manual.handle("sync", {})
    assert (await manual.end_call("x")).status == "manual"
    assert await manual.fetch_recording("x") is None
    assert await manual.fetch_transcript("x") is None
    assert manual.verify_webhook({}, b"{}") is False and manual.parse_webhook({}, b"{}") == {}


async def test_config_category_check_accepts_voice() -> None:
    tenant_id = await create_tenant_row()
    async with tenant_session(tenant_id) as db:
        db.add(
            ProviderConnectorConfig(
                tenant_id=tenant_id, category="voice", provider_key="fake", display_name="V"
            )
        )
        await db.flush()
