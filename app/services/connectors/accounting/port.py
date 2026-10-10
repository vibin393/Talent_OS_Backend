"""``accounting`` provider port (AD-29): the methods of Epic 30 A10.

Owned by Epic 30 after the foundation (R16). Every adapter also implements the generic
``ConnectorPort`` (``handle`` / ``verify_webhook`` / ``parse_webhook``).

Epic 66 (spec 0.9, FR-368) adds ``AccountingSyncPort`` (ledger sync; Epic 71 fills real adapters) and its DTOs.
``AccountingExporter.export`` is unchanged. The Manual adapter stubs the new methods (``NotImplementedError``); the
Fake implements them.
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable


@runtime_checkable
class AccountingExporter(Protocol):
    key: str

    async def export(self, rows: Sequence[Mapping[str, Any]], template: Mapping[str, Any]) -> bytes: ...


@dataclasses.dataclass(frozen=True, slots=True)
class LedgerDocumentDTO:
    document_id: uuid.UUID
    document_version: int
    kind: Literal["invoice", "credit_note", "debit_note", "payment"]
    number: str
    issue_date: date
    legal_entity_id: uuid.UUID
    customer_ref: str | None
    payload: Mapping[str, Any]  # mapped ledger structure (GST, HSN/SAC, IRN)


@dataclasses.dataclass(frozen=True, slots=True)
class LedgerResultDTO:
    document_id: uuid.UUID
    external_ref: str | None
    status: Literal["synced", "pending", "failed", "skipped"]
    message: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class LedgerPaymentDTO:
    external_ref: str
    invoice_number: str
    invoice_external_ref: str | None
    amount: Decimal
    currency: str
    paid_on: date
    tds_amount: Decimal | None
    mode: str | None
    reference: str | None


@runtime_checkable
class AccountingSyncPort(Protocol):
    async def push_documents(self, docs: Sequence[LedgerDocumentDTO]) -> Sequence[LedgerResultDTO]: ...

    async def document_status(self, refs: Sequence[str]) -> Sequence[LedgerResultDTO]: ...

    async def pull_payments(
        self, since: datetime | None, cursor: str | None
    ) -> tuple[Sequence[LedgerPaymentDTO], str | None]: ...

    async def masters_sync(
        self, customers: Sequence[Mapping[str, Any]], ledgers: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, str]: ...


__all__ = [
    "AccountingExporter",
    "AccountingSyncPort",
    "Any",
    "LedgerDocumentDTO",
    "LedgerPaymentDTO",
    "LedgerResultDTO",
    "Mapping",
    "Sequence",
]
