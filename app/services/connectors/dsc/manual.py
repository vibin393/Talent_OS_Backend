"""Manual ``dsc`` connector (AD-29): signing happens outside Talent OS; ``sign_pdf`` asks a person to upload
the externally signed PDF (``ManualActionRequired``)."""

from collections.abc import Mapping
from typing import Any

from app.services.connectors.base import ConnectorConfig, ManualActionRequired
from app.services.connectors.dsc.port import DscSignRequestDTO, DscSignResultDTO
from app.services.connectors.registry import register_adapter


class ManualDscSigner:
    category = "dsc"
    provider_key = "manual"
    key = "manual"

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(f"dsc: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def sign_pdf(self, req: DscSignRequestDTO) -> DscSignResultDTO:
        raise ManualActionRequired(
            "dsc: sign the PDF with the DSC token and upload the externally signed PDF",
            {"op": "sign_pdf", "file_id": str(req.file_id), "sha256": req.sha256},
        )


register_adapter("dsc", "manual", ManualDscSigner)

__all__ = ["ManualDscSigner"]
