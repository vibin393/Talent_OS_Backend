"""Receivables, collections and period close (Epic 30).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = [
    "DisputeStatusChanged",
    "InvoiceWrittenOff",
    "PaymentRecorded",
    "PaymentReversed",
    "PromiseToPayBroken",
    "TdsReconRunCompleted",
]


@dataclasses.dataclass(frozen=True, slots=True)
class PaymentRecorded:
    tenant_id: uuid.UUID
    payment_id: uuid.UUID
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class PaymentReversed:
    tenant_id: uuid.UUID
    payment_id: uuid.UUID
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class PromiseToPayBroken:
    tenant_id: uuid.UUID
    promise_id: uuid.UUID
    invoice_id: uuid.UUID
    client_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class InvoiceWrittenOff:
    tenant_id: uuid.UUID
    write_off_id: uuid.UUID
    document_kind: str
    document_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class DisputeStatusChanged:
    tenant_id: uuid.UUID
    dispute_id: uuid.UUID
    invoice_id: uuid.UUID
    from_status: str
    to_status: str


# Epic 51 (FR-254) domain-local: a 26AS / AIS reconciliation run finished parsing and proposing matches.
@dataclasses.dataclass(frozen=True, slots=True)
class TdsReconRunCompleted:
    tenant_id: uuid.UUID
    run_id: uuid.UUID
