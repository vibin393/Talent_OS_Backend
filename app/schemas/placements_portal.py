"""Worker ("My Work") portal DTOs (Epic 19 A8). Never bill rate, vendor rate, margin, burden or internal notes
(field-visibility guard, FR-80); the Worker's own pay rate is shown."""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.core.field_visibility import register_portal_schema
from app.core.money import DecimalStr


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


@register_portal_schema("worker")
class PortalPlacementOut(BaseModel):
    id: uuid.UUID
    human_id: str
    status: str
    client_name: str | None
    role_title: str | None
    work_location_text: str | None
    planned_start_date: date
    planned_end_date: date | None
    actual_start_date: date | None
    joining_confirmed_at: datetime | None
    can_confirm_joining: bool
    pay_rate: DecimalStr | None
    rate_unit: str | None
    currency: str


@register_portal_schema("worker")
class PortalProfileOut(BaseModel):
    worker_human_id: str | None
    full_name: str | None
    email: str | None
    emergency_name: str | None
    emergency_relation: str | None
    emergency_phone: str | None
    pan_last4: str | None


class PortalProfileUpdate(_In):
    emergency_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    emergency_relation: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    emergency_phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None


@register_portal_schema("worker")
class PortalBankAccountOut(BaseModel):
    id: uuid.UUID
    holder_name: str | None
    account_number_last4: str | None
    ifsc: str
    bank_name: str | None
    status: str
    created_at: datetime


class JoiningResponseIn(_In):
    response: Literal["confirmed", "question"]
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


@register_portal_schema("worker")
class JoiningLinkInfo(BaseModel):
    """Purpose ``joining_confirmation`` of ``GET /public/links/{token}`` (no ids, no rates)."""

    client_name: str | None
    role_title: str | None
    planned_start_date: date
    candidate_first_name: str | None
    responded: bool
