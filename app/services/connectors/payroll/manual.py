"""Manual ``payroll`` connector: the complete human workflow (AD-29). Every operation asks a person to act
(``ManualActionRequired`` -> the job succeeds with ``{"manual": true}``); there are no webhooks."""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.connectors.base import ConnectorConfig, ManualActionRequired
from app.services.connectors.registry import register_adapter


class ManualPayrollProvider:
    category = "payroll"
    provider_key = "manual"
    key = "manual"

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise ManualActionRequired(f"payroll: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # noqa: ARG002
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # noqa: ARG002
        return {}

    async def push_export(self, export_id: uuid.UUID, file: bytes, media_type: str) -> Mapping[str, Any]:  # noqa: ARG002
        raise ManualActionRequired(
            "payroll: upload the export file to the payroll provider by hand",
            {"op": "push_export", "export_id": str(export_id), "media_type": media_type},
        )

    async def get_status(self, external_ref: str) -> Any:  # noqa: ARG002
        raise ManualActionRequired("payroll: get_status is handled manually", {"op": "get_status"})


register_adapter("payroll", "manual", ManualPayrollProvider)

__all__ = ["ManualPayrollProvider", "Sequence"]
