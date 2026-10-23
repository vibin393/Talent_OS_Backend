"""Manual ``accounting`` connector: the complete human workflow (AD-29). Every operation asks a person to act
(``ManualActionRequired`` -> the job succeeds with ``{"manual": true}``); there are no webhooks."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.services.connectors.accounting.port import LedgerDocumentDTO, LedgerPaymentDTO, LedgerResultDTO
from app.services.connectors.base import ConnectorConfig, ManualActionRequired
from app.services.connectors.registry import register_adapter


class ManualAccountingExporter:
    category = "accounting"
    provider_key = "manual"
    key = "manual"

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise ManualActionRequired(f"accounting: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def export(self, rows: Sequence[Mapping[str, Any]], template: Mapping[str, Any]) -> bytes:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired("accounting: export is handled manually", {"op": "export"})

    # ---------------------------------------------------------------- AccountingSyncPort (Epic 66 stubs; Epic 71)
    async def push_documents(self, docs: Sequence[LedgerDocumentDTO]) -> Sequence[LedgerResultDTO]:
        raise NotImplementedError("accounting: push_documents (Epic 71)")

    async def document_status(self, refs: Sequence[str]) -> Sequence[LedgerResultDTO]:
        raise NotImplementedError("accounting: document_status (Epic 71)")

    async def pull_payments(
        self, since: datetime | None, cursor: str | None
    ) -> tuple[Sequence[LedgerPaymentDTO], str | None]:
        raise NotImplementedError("accounting: pull_payments (Epic 71)")

    async def masters_sync(
        self, customers: Sequence[Mapping[str, Any]], ledgers: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, str]:
        raise NotImplementedError("accounting: masters_sync (Epic 71)")


register_adapter("accounting", "manual", ManualAccountingExporter)

__all__ = ["ManualAccountingExporter", "Sequence"]
