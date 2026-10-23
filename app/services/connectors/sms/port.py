# ruff: file-ignore[line-too-long]
"""``sms`` provider port (AD-29, Epic 33 A4; owned by Epic 33 after the foundation, R16).

The adapter factory binds the ``ConnectorConfig`` (credentials only from ``provider_connector_configs``), so the
methods take no ``config``. Every adapter also implements the generic ``ConnectorPort``:

* ``handle(op, payload)`` -- run by ``connector_jobs`` (see ``channel.ChannelAdapter``: op ``send`` re-checks the
  message through the messaging send gate, then calls ``send``);
* ``verify_webhook(headers, body)`` -- signature check (401 otherwise);
* ``parse_webhook(headers, body)`` -- returns ``{"events": [SmsEvent as dict, ...]}`` (the foundation stores the
  parsed mapping in ``connector_inbound_events.payload``; ``events_from_payload`` reads it back).
"""

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

SendStatus = Literal["sent", "delivered", "queued"]


@dataclasses.dataclass(frozen=True, slots=True)
class SendResult:
    provider_message_id: str | None
    status: SendStatus = "sent"


@dataclasses.dataclass(frozen=True, slots=True)
class ChannelEvent:
    """A delivery receipt (``kind='delivery'``) or an inbound message (``kind='inbound'``)."""

    kind: Literal["delivery", "inbound"]
    provider_message_id: str | None = None
    status: str | None = None  # delivery: sent | delivered | read | failed
    error_code: str | None = None
    from_e164: str | None = None
    to_e164: str | None = None
    text: str | None = None
    received_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        out["received_at"] = self.received_at.isoformat() if self.received_at else None
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChannelEvent":
        raw_at = data.get("received_at")
        at = datetime.fromisoformat(str(raw_at)) if raw_at else None
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in fields and k != "received_at"}, received_at=at)


SmsEvent = ChannelEvent


class PermanentSendError(Exception):
    """The provider refused the message for good (invalid number, template rejected): no retry."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code[:64]
        super().__init__(message or code)


def events_from_payload(payload: Mapping[str, Any]) -> list[ChannelEvent]:
    return [ChannelEvent.from_dict(e) for e in payload.get("events") or [] if isinstance(e, Mapping)]


@runtime_checkable
class SmsProvider(Protocol):
    category: str
    provider_key: str

    async def send(
        self,
        to_e164: str,
        sender_id: str | None,
        dlt_template_id: str,
        dlt_entity_id: str | None,
        variables: Sequence[str],
        body: str,
    ) -> SendResult: ...

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool: ...

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]: ...


__all__ = [
    "ChannelEvent",
    "PermanentSendError",
    "SendResult",
    "SmsEvent",
    "SmsProvider",
    "events_from_payload",
]
