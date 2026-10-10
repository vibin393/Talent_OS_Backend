# ruff: noqa: E501, D401
"""Bridged-call operations (Epic 76, spec A2; additive next to the frozen ``port.py``, frozen once merged).

A bridged call rings the recruiter's verified phone first, then the Candidate, from the Tenant's caller ID. Real
telephony adapters (Exotel, Twilio, Plivo) and the Fake run these through ``ConnectorPort.handle(op, payload)``; the
payload is ``dataclasses.asdict(BridgedCallRequest)`` (``to_payload``) and results are plain mappings
(``BridgedCallResult`` / ``CallStatusDTO`` via ``from_mapping``).

Normalised webhook payload (``parse_webhook``): ``{external_ref, kind, leg, provider_status, external_event_id,
recording_ref?, recording_channels?, duration_s?, price_minor?, currency?}`` with ``kind`` in ``EVENT_KINDS``.
"""

import dataclasses
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

__all__ = [
    "EVENT_KINDS",
    "OP_BRIDGE_CALL",
    "OP_CALL_STATUS",
    "OP_DELETE_RECORDING",
    "OP_HANGUP",
    "BridgedCallRequest",
    "BridgedCallResult",
    "CallStatusDTO",
    "billable_minutes",
    "to_minor",
]

OP_BRIDGE_CALL = "voice.bridge_call"  # place the bridged call
OP_HANGUP = "voice.hangup"
OP_CALL_STATUS = "voice.call_status"  # verify-by-fetch (Exotel) and reconciliation
OP_DELETE_RECORDING = "voice.delete_recording"

EVENT_KINDS = (
    "ringing", "answered", "candidate_answered", "completed", "no_answer", "busy", "failed", "recording_ready",
)  # fmt: skip


@dataclasses.dataclass(frozen=True, slots=True)
class BridgedCallRequest:
    voice_call_id: uuid.UUID
    agent_e164: str
    candidate_e164: str
    caller_id_e164: str
    record: bool
    dual_channel: bool
    notice_text: str | None  # spoken when record=True
    status_callback_url: str
    answer_url: str | None  # for TwiML / XML providers
    max_duration_s: int
    language: str = "en-IN"

    def to_payload(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        out["voice_call_id"] = str(self.voice_call_id)
        return out

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BridgedCallRequest":
        data = {f.name: payload.get(f.name) for f in dataclasses.fields(cls) if f.name in payload}
        data["voice_call_id"] = uuid.UUID(str(payload["voice_call_id"]))
        return cls(**data)

    def __repr__(self) -> str:  # never print phone numbers
        return f"BridgedCallRequest(voice_call_id={self.voice_call_id}, record={self.record})"


@dataclasses.dataclass(frozen=True, slots=True)
class BridgedCallResult:
    external_ref: str
    status: Literal["dialing_recruiter", "failed"]
    failure_code: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "BridgedCallResult":
        return cls(str(data.get("external_ref") or ""), data.get("status") or "failed", data.get("failure_code"))


@dataclasses.dataclass(frozen=True, slots=True)
class CallStatusDTO:
    external_ref: str
    status: str  # one of EVENT_KINDS (normalised) or "in_progress" / "queued"
    answered_at: datetime | None
    ended_at: datetime | None
    duration_s: int | None
    price_minor: int | None
    currency: str | None
    recording_ref: str | None
    recording_channels: int | None

    def to_mapping(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        for key in ("answered_at", "ended_at"):
            if out[key] is not None:
                out[key] = out[key].isoformat()
        return out

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CallStatusDTO":
        def dt(v: Any) -> datetime | None:
            if v is None or isinstance(v, datetime):
                return v
            return datetime.fromisoformat(str(v))

        return cls(
            external_ref=str(data.get("external_ref") or ""),
            status=str(data.get("status") or ""),
            answered_at=dt(data.get("answered_at")),
            ended_at=dt(data.get("ended_at")),
            duration_s=data.get("duration_s"),
            price_minor=data.get("price_minor"),
            currency=data.get("currency"),
            recording_ref=data.get("recording_ref"),
            recording_channels=data.get("recording_channels"),
        )


def billable_minutes(duration_s: int | None) -> int:
    """Ceil of the provider duration in minutes (0 for no duration)."""
    if not duration_s or duration_s <= 0:
        return 0
    return (int(duration_s) + 59) // 60


def to_minor(amount: Any) -> int | None:
    """A provider price (``"-0.0130"``, ``0.65``) as absolute minor units (x100, rounded)."""
    if amount in (None, ""):
        return None
    try:
        from decimal import ROUND_HALF_UP, Decimal

        return int((abs(Decimal(str(amount))) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except Exception:
        return None
