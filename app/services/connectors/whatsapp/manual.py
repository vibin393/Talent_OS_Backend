"""Manual ``whatsapp`` connector: the complete human workflow without a provider (AD-29, NFR-31). Sends become
``manual_pending`` in the operator queue; templates are entered by hand. No webhooks."""

from collections.abc import Mapping, Sequence
from typing import Any

from app.services.connectors.base import ManualActionRequired
from app.services.connectors.registry import register_adapter
from app.services.connectors.sms.channel import ChannelAdapter, SendSpec
from app.services.connectors.sms.port import SendResult
from app.services.connectors.whatsapp.port import ProviderTemplate


class ManualWhatsAppProvider(ChannelAdapter):
    category = "whatsapp"
    provider_key = "manual"

    async def _deliver(self, spec: SendSpec) -> SendResult:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(
            "whatsapp: send this template message by hand from the manual queue", {"op": "send"}
        )

    async def send_template(
        self, to_e164: str, template_name: str, language: str, variables: Sequence[str]
    ) -> SendResult:
        return await self._deliver(
            SendSpec("whatsapp", to_e164, "", template_name, list(variables), language)
        )

    async def list_templates(self) -> list[ProviderTemplate]:
        return []  # Manual connector: templates are entered by hand

    async def _other(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(f"whatsapp: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {"events": []}


register_adapter("whatsapp", "manual", ManualWhatsAppProvider)

__all__ = ["ManualWhatsAppProvider"]
