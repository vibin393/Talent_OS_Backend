"""Manual ``fx`` connector (AD-29): no feed; ``fetch_rates`` returns ``[]`` so tenant-entered ``fx_rates``
stay authoritative."""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.services.connectors.base import ConnectorConfig, ManualActionRequired
from app.services.connectors.fx.port import FxRateDTO
from app.services.connectors.registry import register_adapter


class ManualFxRateSource:
    category = "fx"
    provider_key = "manual"
    key = "manual"

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(f"fx: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def fetch_rates(self, on: date, pairs: Sequence[tuple[str, str]]) -> Sequence[FxRateDTO]:  # ruff: ignore[unused-method-argument]  # NOSONAR
        return []


register_adapter("fx", "manual", ManualFxRateSource)

__all__ = ["ManualFxRateSource"]
