"""Worker portal ("My Work") signature schemas (Epic 22)."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.field_visibility import register_portal_schema


@register_portal_schema("worker")
class PortalSignatureOut(BaseModel):
    request_id: uuid.UUID
    signer_id: uuid.UUID
    human_id: str
    title: str
    status: str
    signer_status: str
    your_turn: bool
    expires_at: datetime
    completed_at: datetime | None
    final_available: bool


@register_portal_schema("worker")
class OfferOtpStatusOut(BaseModel):
    required: bool
