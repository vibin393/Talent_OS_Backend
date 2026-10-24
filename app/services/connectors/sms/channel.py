# ruff: file-ignore[line-too-long]
"""Shared job plumbing of the ``sms`` and ``whatsapp`` adapters (Epic 33, AD-29).

``connector_jobs`` call ``adapter.handle("send", {"tenant_id", "channel_message_id", ...})``. Before any provider
call the adapter asks the **send gate** -- registered by the messaging domain (``register_send_gate``), so this
package never imports it (AD-32) -- to re-check the message (status, opt-out, WhatsApp consent, quiet hours) and
to hand over what to send. After the call the gate records the outcome (status, provider id, Activity).

* ``ManualActionRequired`` (Manual connector) -> outcome ``manual_pending``; the job ends ``succeeded``.
* ``PermanentSendError`` -> outcome ``failed`` with ``error_code``; no retry.
* any other exception -> re-raised: the job retries with back-off, and ``ConnectorJobFailed`` after the last
  attempt lets the messaging domain mark the message ``failed``.
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from app.services.connectors.base import ConnectorConfig, ManualActionRequired
from app.services.connectors.sms.port import PermanentSendError, SendResult


@dataclasses.dataclass(frozen=True, slots=True)
class SendSpec:
    channel: str
    to_e164: str
    body: str
    template_ref: str
    variables: Sequence[str]
    language: str = "en"
    sender_id: str | None = None
    dlt_entity_id: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Prepared:
    spec: SendSpec | None
    skip_reason: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Outcome:
    status: str  # sent | delivered | manual_pending | failed
    provider_message_id: str | None = None
    error_code: str | None = None


class SendGate(Protocol):
    async def prepare(self, tenant_id: uuid.UUID, message_id: uuid.UUID) -> Prepared: ...

    async def complete(self, tenant_id: uuid.UUID, message_id: uuid.UUID, outcome: Outcome) -> None: ...


_gate: SendGate | None = None


def register_send_gate(gate: SendGate | None) -> None:
    global _gate  # process-wide wiring (messaging hooks)
    _gate = gate


def send_gate() -> SendGate | None:
    return _gate


class ChannelAdapter:
    """Base of every sms / whatsapp adapter: ``handle`` drives the gate; subclasses implement ``_deliver``."""

    category = ""
    provider_key = ""

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    @property
    def key(self) -> str:
        return self.provider_key

    async def _deliver(self, spec: SendSpec) -> SendResult:
        raise NotImplementedError

    async def _other(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]  # NOSONAR
        msg = f"{self.category}: unsupported operation {op!r}"
        raise ValueError(msg)

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if op == "send_test":
            spec = SendSpec(**dict(payload["spec"]))
            result = await self._deliver(spec)
            return {"provider_message_id": result.provider_message_id, "status": result.status}
        if op != "send" or not payload.get("channel_message_id"):
            return await self._other(op, payload)
        gate = send_gate()
        if gate is None:
            msg = "messaging send gate is not installed"
            raise RuntimeError(msg)
        tenant_id = uuid.UUID(str(payload["tenant_id"]))
        message_id = uuid.UUID(str(payload["channel_message_id"]))
        prepared = await gate.prepare(tenant_id, message_id)
        if prepared.spec is None:
            return {"skipped": prepared.skip_reason}
        try:
            result = await self._deliver(prepared.spec)
        except ManualActionRequired:
            await gate.complete(tenant_id, message_id, Outcome("manual_pending"))
            raise
        except PermanentSendError as exc:
            await gate.complete(tenant_id, message_id, Outcome("failed", error_code=exc.code))
            return {"failed": exc.code}
        await gate.complete(
            tenant_id, message_id, Outcome(result.status, provider_message_id=result.provider_message_id)
        )
        return {"provider_message_id": result.provider_message_id, "status": result.status}


def mask_phone(phone: str | None) -> str | None:
    """``+91******3210`` -- the only form in which a number may appear in logs / list views."""
    if not phone:
        return None
    return f"{phone[:3]}{'*' * max(len(phone) - 7, 0)}{phone[-4:]}" if len(phone) > 7 else "*" * len(phone)
