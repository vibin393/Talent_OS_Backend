"""Ledger sync events (Epic 71, spec A10). Frozen, slotted dataclasses; ``tenant_id`` first; ids and statuses only."""

import dataclasses
import uuid

__all__ = ["LedgerSyncStatusChanged", "PaymentSuggestionDecided"]


@dataclasses.dataclass(frozen=True, slots=True)
class LedgerSyncStatusChanged:
    tenant_id: uuid.UUID
    record_id: uuid.UUID
    document_kind: str
    document_id: uuid.UUID
    status: str


@dataclasses.dataclass(frozen=True, slots=True)
class PaymentSuggestionDecided:
    tenant_id: uuid.UUID
    suggestion_id: uuid.UUID
    outcome: str
    payment_id: uuid.UUID | None
