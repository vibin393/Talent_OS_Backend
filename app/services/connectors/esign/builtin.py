"""Built-in typed-name eSign provider (Epic 22 A8): the default when no ``esign`` connector is configured.

``create_request`` returns the request id (signing runs in-app through ``documents.esign``); ``get_status`` and
``fetch_signed`` are answered from the local rows by the documents domain, so they report "unknown" here.
"""

from collections.abc import Mapping
from typing import Any

from app.services.connectors.base import ConnectorConfig
from app.services.connectors.esign.port import EsignRequestDTO, EsignStatusDTO, EsignWebhookDTO
from app.services.connectors.registry import register_adapter


class BuiltinEsignProvider:
    category = "esign"
    provider_key = "builtin"
    key = "builtin"
    in_app = True

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"ok": True, "op": op, "builtin": True, "payload_keys": sorted(payload)}

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def create_request(self, req: EsignRequestDTO) -> str:
        return str(req.request_id)

    async def get_status(self, external_ref: str) -> EsignStatusDTO:  # ruff: ignore[unused-method-argument]
        return EsignStatusDTO(status="unknown")

    async def fetch_signed(self, external_ref: str) -> bytes | None:  # ruff: ignore[unused-method-argument]
        return None

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> EsignWebhookDTO:
        msg = "the built-in provider has no webhooks"
        raise NotImplementedError(msg)


register_adapter("esign", "builtin", BuiltinEsignProvider)

__all__ = ["BuiltinEsignProvider"]
