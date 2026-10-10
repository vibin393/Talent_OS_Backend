"""Billing & GST invoicing (Epic 29; ``InvoiceOverdue`` is emitted by Epic 30's daily beat).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid
from datetime import date

__all__ = [
    "CreditNoteIssued",
    "DebitNoteIssued",
    "DocumentDscSigned",
    "EinvoiceStatusChanged",
    "InvoiceCancelled",
    "InvoiceIssued",
    "InvoiceOverdue",
    "InvoiceRunCompleted",
]


@dataclasses.dataclass(frozen=True, slots=True)
class InvoiceIssued:
    tenant_id: uuid.UUID
    invoice_id: uuid.UUID
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    number: str
    doc_date: date


@dataclasses.dataclass(frozen=True, slots=True)
class InvoiceCancelled:
    tenant_id: uuid.UUID
    invoice_id: uuid.UUID
    cancel_basis: str


@dataclasses.dataclass(frozen=True, slots=True)
class CreditNoteIssued:
    tenant_id: uuid.UUID
    credit_note_id: uuid.UUID
    invoice_id: uuid.UUID
    client_id: uuid.UUID
    rebill: bool


@dataclasses.dataclass(frozen=True, slots=True)
class DebitNoteIssued:
    tenant_id: uuid.UUID
    debit_note_id: uuid.UUID
    invoice_id: uuid.UUID
    client_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class InvoiceRunCompleted:
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    legal_entity_id: uuid.UUID
    draft_count: int
    exception_count: int


@dataclasses.dataclass(frozen=True, slots=True)
class EinvoiceStatusChanged:
    tenant_id: uuid.UUID
    document_kind: str
    document_id: uuid.UUID
    status: str


@dataclasses.dataclass(frozen=True, slots=True)
class InvoiceOverdue:
    tenant_id: uuid.UUID
    invoice_id: uuid.UUID
    client_id: uuid.UUID
    days_overdue: int


# Epic 51 (FR-253) domain-local: an issued invoice / credit note got its DSC-signed PDF (document version 2).
@dataclasses.dataclass(frozen=True, slots=True)
class DocumentDscSigned:
    tenant_id: uuid.UUID
    kind: str  # invoice | credit_note
    doc_id: uuid.UUID
