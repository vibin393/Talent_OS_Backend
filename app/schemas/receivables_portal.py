"""Client Portal receivables DTOs (Epic 30): statements and payment status only. Collection activities, disputes'
internal notes, write-off requests and settlement internals never appear here."""

import uuid
from datetime import date

from pydantic import BaseModel

from app.core.field_visibility import register_portal_schema
from app.core.money import DecimalStr
from app.schemas.receivables import StatementOut


@register_portal_schema("client_user")
class PortalPaymentStatus(BaseModel):
    document_kind: str
    document_id: uuid.UUID
    open_amount: DecimalStr
    payment_status: str
    due_date: date | None


@register_portal_schema("client_user")
class PortalStatement(StatementOut):
    pass
