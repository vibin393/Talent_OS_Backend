"""Client Portal billing DTOs (Epic 29): issued documents only; no manual flags, postings or notes."""

import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel

from app.core.field_visibility import register_portal_schema
from app.core.money import DecimalStr


@register_portal_schema("client_user")
class PortalInvoiceSummary(BaseModel):
    id: uuid.UUID
    kind: Literal["invoice", "credit_note", "debit_note"]
    number: str | None
    doc_date: date | None
    due_date: date | None
    currency: str
    document_total: DecimalStr
    status: str
    payment_status: str | None


@register_portal_schema("client_user")
class PortalInvoiceDetail(PortalInvoiceSummary):
    taxable_total: DecimalStr
    tax_total: DecimalStr
    amount_in_words: str
    irn: str | None
    lines: list[dict[str, Any]]
    # Epic 51: False while the Client's DSC policy requires a signature that is not there yet.
    pdf_available: bool = True
