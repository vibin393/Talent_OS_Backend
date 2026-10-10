# ruff: noqa: E501
"""Fake ``voice`` connector for tests (AD-29, spec 0.13): a scripted, deterministic session lifecycle
(``scheduled`` -> ``started`` -> ``ended`` plus ``recording_ready`` / ``transcript_ready`` events), a scripted
transcript per ``script_ref``, a WAV of silence for the recording and a fake ``TranscriptionProvider``. Records
calls (``calls`` / ``fail_ops`` / ``reset()``); HMAC ``X-Fake-Signature`` webhooks. Registered as ``fake`` outside
production."""

import hashlib
import hmac
import io
import json
import wave
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from app.core.settings import get_settings
from app.services.connectors.base import ConnectorConfig
from app.services.connectors.registry import register_adapter
from app.services.connectors.voice.port import (
    CallEventDTO,
    CallSessionDTO,
    CallSessionRequestDTO,
    RecordingDTO,
    TranscriptDTO,
    TranscriptSegmentDTO,
)

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
RECORDING_SECONDS = 1
_SAMPLE_RATE = 8000

SCRIPTS: dict[str, tuple[TranscriptSegmentDTO, ...]] = {
    "default": (
        TranscriptSegmentDTO("agent", 0, 4000, "Hello, this is the screening assistant. Is now a good time?"),
        TranscriptSegmentDTO("candidate", 4500, 6000, "Yes, it is."),
        TranscriptSegmentDTO("agent", 6500, 10000, "How many years of Python experience do you have?"),
        TranscriptSegmentDTO("candidate", 10500, 13000, "About five years."),
    )
}


def silence_wav(seconds: int = RECORDING_SECONDS, channels: int = 1) -> bytes:
    """Return a 16-bit PCM WAV of silence (deterministic bytes); ``channels=2`` for a dual-channel call (Epic 76)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(b"\x00\x00" * channels * _SAMPLE_RATE * seconds)
    return buf.getvalue()


def script_transcript(script_ref: str | None) -> TranscriptDTO:
    return TranscriptDTO(segments=SCRIPTS.get(script_ref or "default", SCRIPTS["default"]), language="en")


class FakeVoiceProvider:
    category = "voice"
    provider_key = "fake"
    key = "fake"
    calls: ClassVar[list[tuple[str, Any]]] = []
    fail_ops: ClassVar[set[str]] = set()
    sessions: ClassVar[dict[str, dict[str, Any]]] = {}

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()
        cls.fail_ops.clear()
        cls.sessions.clear()

    def _record(self, op: str, args: Any) -> None:
        type(self).calls.append((op, args))
        if op in type(self).fail_ops:
            msg = f"fake {op} failure"
            raise RuntimeError(msg)

    # ---------------------------------------------------------------- generic port
    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._record(op, dict(payload))
        digest = hashlib.sha256(json.dumps(dict(payload), sort_keys=True, default=str).encode()).hexdigest()
        return {"ok": True, "op": op, "ref": f"fake-{digest[:12]}"}

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        secret = self.config.webhook_secret if self.config is not None else None
        if not secret:
            return False
        given = {k.lower(): v for k, v in headers.items()}.get("x-fake-signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(given, expected)

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # noqa: ARG002
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"data": data}

    # ---------------------------------------------------------------- voice port
    async def schedule_call(self, req: CallSessionRequestDTO) -> CallSessionDTO:
        self._record("schedule_call", req)
        ref = f"fake-call:{req.voice_session_id}"
        type(self).sessions[ref] = {"record": req.record, "script_ref": req.script_ref or "default"}
        return CallSessionDTO(external_ref=ref, status="scheduled")

    async def start_call(self, external_ref: str) -> CallSessionDTO:
        self._record("start_call", external_ref)
        return CallSessionDTO(external_ref=external_ref, status="started")

    async def end_call(self, external_ref: str) -> CallSessionDTO:
        self._record("end_call", external_ref)
        session = type(self).sessions.get(external_ref, {"record": False, "script_ref": "default"})
        return CallSessionDTO(
            external_ref=external_ref,
            status="ended",
            recording_ref=f"rec:{external_ref}" if session["record"] else None,
            transcript_ref=f"tx:{session['script_ref']}",
        )

    async def fetch_recording(self, recording_ref: str) -> RecordingDTO | None:
        self._record("fetch_recording", recording_ref)
        if not recording_ref.startswith("rec:"):
            return None
        return RecordingDTO(data=silence_wav(), media_type="audio/wav", duration_s=RECORDING_SECONDS)

    async def fetch_transcript(self, transcript_ref: str) -> TranscriptDTO | None:
        self._record("fetch_transcript", transcript_ref)
        if not transcript_ref.startswith("tx:"):
            return None
        return script_transcript(transcript_ref.removeprefix("tx:"))

    def lifecycle(self, external_ref: str, *, record: bool = True) -> tuple[CallEventDTO, ...]:
        """Return the deterministic event sequence of a scripted session."""
        kinds: list[Any] = ["scheduled", "started", "ended"]
        if record:
            kinds.append("recording_ready")
        kinds.append("transcript_ready")
        return tuple(
            CallEventDTO(external_ref=external_ref, kind=k, occurred_at=EPOCH + timedelta(seconds=i))
            for i, k in enumerate(kinds)
        )


class FakeTranscriptionProvider:
    """Fake STT capability: returns the ``default`` script whatever the audio (records the call)."""

    calls: ClassVar[list[tuple[str, Any]]] = []

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()

    async def transcribe(self, audio: bytes, media_type: str, language_hint: str | None) -> TranscriptDTO:
        type(self).calls.append(("transcribe", (len(audio), media_type, language_hint)))
        dto = script_transcript("default")
        return TranscriptDTO(segments=dto.segments, language=language_hint or dto.language)


class FakeBridgedVoiceProvider(FakeVoiceProvider):
    """Epic 76 (spec A2, additive): the manifest Fake of the real telephony providers (``AdapterContext``). Bridged
    calls through ``handle(OP_BRIDGE_CALL)`` return ``FKC-<n>`` and script deterministic events chosen by the
    Candidate number suffix: ``...0002`` no answer, ``...0003`` busy, ``...0004`` failed, ``...0005`` recruiter no
    answer, anything else answered (``ringing -> answered -> candidate_answered -> completed`` + ``recording_ready`` when
    recording). ``bridged_events(ref)`` returns the normalised webhook payloads; ``OP_CALL_STATUS`` the final status;
    ``OP_DELETE_RECORDING`` is recorded in ``deleted``. Webhooks: HMAC ``X-Fake-Signature``. No socket is opened."""

    bridged: ClassVar[dict[str, dict[str, Any]]] = {}
    deleted: ClassVar[list[str]] = []
    counter: ClassVar[list[int]] = [0]
    DURATION_S = 75

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(None)
        self.ctx = ctx

    def __repr__(self) -> str:
        return "FakeBridgedVoiceProvider()"

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.bridged.clear()
        cls.deleted.clear()
        cls.counter[0] = 0

    @staticmethod
    def script_for(candidate_e164: str) -> str:
        return {"0002": "no_answer", "0003": "busy", "0004": "failed", "0005": "recruiter_no_answer"}.get(
            candidate_e164[-4:], "answered"
        )

    def _secret(self) -> str | None:
        secrets = getattr(self.ctx, "secrets", None)
        if secrets is None:
            return None
        for name in ("webhook_secret", "auth_token", "webhook_path_token", "api_token"):
            value = secrets.get(name)
            if value:
                return str(value)
        return None

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        cls = type(self)
        if op == "voice.bridge_call":
            self._record(op, {"voice_call_id": payload.get("voice_call_id"), "record": payload.get("record")})
            cls.counter[0] += 1
            ref = f"FKC-{cls.counter[0]}"
            cls.bridged[ref] = {
                "script": self.script_for(str(payload.get("candidate_e164") or "")),
                "record": bool(payload.get("record")),
                "dual": bool(payload.get("dual_channel")),
                "payload": {k: v for k, v in payload.items() if k not in ("agent_e164", "candidate_e164")},
            }
            return {"external_ref": ref, "status": "dialing_recruiter"}
        if op == "voice.call_status":
            ref = str(payload.get("external_ref") or "")
            self._record(op, ref)
            events = self.bridged_events(ref)
            last = next((e for e in reversed(events) if e["kind"] != "recording_ready"), None)
            done = last is not None and last["kind"] in ("completed", "no_answer", "busy", "failed")
            record = bool(cls.bridged.get(ref, {}).get("record"))
            return {
                "external_ref": ref, "status": last["kind"] if last else "", "answered_at": None,
                "ended_at": EPOCH.isoformat() if done else None,
                "duration_s": last.get("duration_s") if last else None, "price_minor": None, "currency": None,
                "recording_ref": f"FKREC-{ref}" if record else None, "recording_channels": 2 if record else None,
            }  # fmt: skip
        if op == "voice.hangup":
            self._record(op, payload.get("external_ref"))
            return {"ok": True}
        if op == "voice.delete_recording":
            ref = str(payload.get("recording_ref") or "")
            self._record(op, ref)
            cls.deleted.append(ref)
            return {"status": "deleted"}
        if op == "health":
            self._record(op, None)
            return {"ok": True}
        return await super().handle(op, payload)

    def bridged_events(self, external_ref: str) -> list[dict[str, Any]]:
        """Return the normalised webhook payloads of a scripted bridged call (deterministic)."""
        state = type(self).bridged.get(external_ref)
        if state is None:
            return []
        script = state["script"]
        if script == "recruiter_no_answer":
            kinds = [("ringing", "agent"), ("no_answer", "agent")]
        elif script == "answered":
            kinds = [("ringing", "agent"), ("answered", "agent"), ("candidate_answered", "candidate"), ("completed", "agent")]
        else:
            kinds = [("ringing", "agent"), ("answered", "agent"), (script, "candidate")]
        out: list[dict[str, Any]] = []
        for i, (kind, leg) in enumerate(kinds):
            event: dict[str, Any] = {
                "external_ref": external_ref, "kind": kind, "leg": leg, "provider_status": kind,
                "external_event_id": f"{external_ref}:{i}:{kind}",
            }  # fmt: skip
            if kind == "completed":
                event["duration_s"] = self.DURATION_S
            out.append(event)
        if script == "answered" and state["record"]:
            out.append({
                "external_ref": external_ref, "kind": "recording_ready", "leg": "agent", "provider_status": "recorded",
                "external_event_id": f"{external_ref}:rec", "recording_ref": f"FKREC-{external_ref}",
                "recording_channels": 2 if state["dual"] else 1,
            })  # fmt: skip
        return out

    async def fetch_recording(self, recording_ref: str) -> RecordingDTO | None:
        if recording_ref.startswith("FKREC-"):
            self._record("fetch_recording", recording_ref)
            dual = type(self).bridged.get(recording_ref.removeprefix("FKREC-"), {}).get("dual", True)
            return RecordingDTO(
                data=silence_wav(channels=2 if dual else 1), media_type="audio/wav", duration_s=RECORDING_SECONDS
            )
        return await super().fetch_recording(recording_ref)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        secret = self._secret()
        if not secret:
            return False
        given = {k.lower(): v for k, v in headers.items()}.get("x-fake-signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(given, expected)


if get_settings().app_env != "production":
    register_adapter("voice", "fake", FakeVoiceProvider)

__all__ = [
    "SCRIPTS",
    "FakeBridgedVoiceProvider",
    "FakeTranscriptionProvider",
    "FakeVoiceProvider",
    "script_transcript",
    "silence_wav",
]
