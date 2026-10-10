"""Client Portal schemas of the demands domain (Epic 18, FR-100). Client-only classes (FR-80): no pay rates,
margin, internal notes, owners or vendor data."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.field_visibility import register_portal_schema
from app.core.money import DecimalStr


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@register_portal_schema("client_user")
class ClientDemandSummary(BaseModel):
    id: uuid.UUID
    req_id: str
    title: str
    positions: int
    status: str
    location: str | None
    start_date: date | None
    sent_submissions: int
    requested_by_me: bool
    requested_via_portal: bool
    created_at: datetime


class ClientDemandRequestIn(_In):
    title: str = Field(min_length=2, max_length=300)
    positions: int = Field(default=1, ge=1, le=500)
    skills: list[str] = Field(default_factory=list, max_length=30)
    location: str | None = Field(default=None, max_length=300)
    start_date: date | None = None
    budget_amount: DecimalStr | None = Field(default=None, ge=0)
    budget_currency: str | None = Field(default=None, min_length=3, max_length=3)
    budget_unit: Literal["hour", "day", "month", "year"] | None = None
    description: str | None = Field(default=None, max_length=20000)


@register_portal_schema("client_user")
class ClientDemandRequestOut(BaseModel):
    id: uuid.UUID
    req_id: str
    title: str
    status: str
    source: Literal["client_portal"] = "client_portal"
    jd_attached: bool = False
