"""Manual ``einvoice`` connector: the complete human workflow (AD-29). Every operation asks a person to act
(``ManualActionRequired`` -> the job succeeds with ``{"manual": true}``); there are no webhooks."""

from collections.abc import Mapping, Sequence
from typing import Any

from app.services.connectors.base import ConnectorConfig, ManualActionRequired
from app.services.connectors.registry import register_adapter


class ManualEInvoiceProvider:
    category = "einvoice"
    provider_key = "manual"
    key = "manual"

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise ManualActionRequired(f"einvoice: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def generate_irn(self, doc_payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired("einvoice: generate_irn is handled manually", {"op": "generate_irn"})

    async def cancel_irn(self, irn: str, reason: str) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired("einvoice: cancel_irn is handled manually", {"op": "cancel_irn"})

    async def get_irn(self, doc_ref: str) -> Mapping[str, Any] | None:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired("einvoice: get_irn is handled manually", {"op": "get_irn"})


register_adapter("einvoice", "manual", ManualEInvoiceProvider)

__all__ = ["ManualEInvoiceProvider", "Sequence"]
