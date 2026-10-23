"""Manual ``esign`` connector: the complete human workflow (AD-29). Every operation asks a person to act
(``ManualActionRequired`` -> the job succeeds with ``{"manual": true}``); there are no webhooks."""

from collections.abc import Mapping, Sequence
from typing import Any

from app.services.connectors.base import ConnectorConfig, ManualActionRequired
from app.services.connectors.registry import register_adapter


class ManualEsignProvider:
    category = "esign"
    provider_key = "manual"
    key = "manual"

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise ManualActionRequired(f"esign: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def create_request(self, req: Any) -> str:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired("esign: create_request is handled manually", {"op": "create_request"})

    async def get_status(self, external_ref: str) -> Any:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired("esign: get_status is handled manually", {"op": "get_status"})

    async def fetch_signed(self, external_ref: str) -> bytes | None:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired("esign: fetch_signed is handled manually", {"op": "fetch_signed"})

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> Any:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired("esign: handle_webhook is handled manually", {"op": "handle_webhook"})


register_adapter("esign", "manual", ManualEsignProvider)

__all__ = ["ManualEsignProvider", "Sequence"]
