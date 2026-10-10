"""Manual ``tds_statement`` connector (AD-29): ``fetch`` asks a person to upload the TRACES statement
(``ManualActionRequired``; the upload is parsed by Epic 51)."""

from collections.abc import Mapping
from typing import Any

from app.services.connectors.base import ConnectorConfig, ManualActionRequired
from app.services.connectors.registry import register_adapter
from app.services.connectors.tds_statement.port import Quarter, TdsStatementDTO


class ManualTdsStatementSource:
    category = "tds_statement"
    provider_key = "manual"
    key = "manual"

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # noqa: ARG002
        raise ManualActionRequired(f"tds_statement: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # noqa: ARG002
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # noqa: ARG002
        return {}

    async def fetch(self, tan: str, fy: str, quarter: Quarter) -> TdsStatementDTO:
        raise ManualActionRequired(
            "tds_statement: download the statement from TRACES and upload it",
            {"op": "fetch", "tan": tan, "fy": fy, "quarter": quarter},
        )


register_adapter("tds_statement", "manual", ManualTdsStatementSource)

__all__ = ["ManualTdsStatementSource"]
