# ruff: noqa: E501
"""Internal outbox event types owned by Epic 71 (spec A7; visibility ``internal``: connector triggers only):
``credit_note.issued``, ``debit_note.issued`` and ``invoice.cancelled`` (``invoice.issued`` / ``payment.recorded`` are
public foundation types)."""

import uuid

from pydantic import BaseModel, ConfigDict

from app.core.public_events import PublicEventSpec, catalogue, register_public_event
from app.events.billing import CreditNoteIssued, DebitNoteIssued, InvoiceCancelled

__all__ = ["register"]


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoteIssuedData(_Data):
    id: uuid.UUID
    invoice_id: uuid.UUID
    client_id: uuid.UUID


class InvoiceCancelledData(_Data):
    id: uuid.UUID
    cancel_basis: str


SPECS = (
    PublicEventSpec(
        type="credit_note.issued", version=1, source=CreditNoteIssued, subject_type="credit_note", subject_id=lambda e: e.credit_note_id,
        data_model=NoteIssuedData, build=lambda e: NoteIssuedData(id=e.credit_note_id, invoice_id=e.invoice_id, client_id=e.client_id),
        scopes=(), visibility="internal", owner_epic="71", description="A credit note was issued.",
    ),
    PublicEventSpec(
        type="debit_note.issued", version=1, source=DebitNoteIssued, subject_type="debit_note", subject_id=lambda e: e.debit_note_id,
        data_model=NoteIssuedData, build=lambda e: NoteIssuedData(id=e.debit_note_id, invoice_id=e.invoice_id, client_id=e.client_id),
        scopes=(), visibility="internal", owner_epic="71", description="A debit note was issued.",
    ),
    PublicEventSpec(
        type="invoice.cancelled", version=1, source=InvoiceCancelled, subject_type="invoice", subject_id=lambda e: e.invoice_id,
        data_model=InvoiceCancelledData, build=lambda e: InvoiceCancelledData(id=e.invoice_id, cancel_basis=e.cancel_basis),
        scopes=(), visibility="internal", owner_epic="71", description="An invoice was cancelled.",
    ),
)  # fmt: skip


def register() -> None:
    known = catalogue(include_internal=True)
    for spec in SPECS:
        if spec.type not in known:
            register_public_event(spec)
