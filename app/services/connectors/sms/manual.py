# ruff: noqa: E501
"""Manual ``sms`` connector: the complete human workflow without a provider (AD-29, NFR-31). A send becomes
``manual_pending`` in the operator queue (``GET /comms/manual-queue``); an operator sends it by hand and marks it
sent / failed. No webhooks."""

from collections.abc import Mapping, Sequence
from typing import Any

from app.services.connectors.base import ManualActionRequired
from app.services.connectors.registry import register_adapter
from app.services.connectors.sms.channel import ChannelAdapter, SendSpec
from app.services.connectors.sms.port import SendResult


class ManualSmsProvider(ChannelAdapter):
    category = "sms"
    provider_key = "manual"

    async def _deliver(self, spec: SendSpec) -> SendResult:  # noqa: ARG002
        raise ManualActionRequired("sms: send this message by hand from the manual queue", {"op": "send"})

    async def send(
        self,
        to_e164: str,
        sender_id: str | None,
        dlt_template_id: str,
        dlt_entity_id: str | None,
        variables: Sequence[str],
        body: str,
    ) -> SendResult:
        return await self._deliver(
            SendSpec("sms", to_e164, body, dlt_template_id, list(variables), "en", sender_id, dlt_entity_id)
        )

    async def _other(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # noqa: ARG002
        raise ManualActionRequired(f"sms: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # noqa: ARG002
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # noqa: ARG002
        return {"events": []}


register_adapter("sms", "manual", ManualSmsProvider)

__all__ = ["ManualSmsProvider"]
