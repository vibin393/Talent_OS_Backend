# ruff: file-ignore[line-too-long]
"""Epic 31 Client Portal and Secure Link schemas. No pay rate, bill rate, vendor rate or margin (frozen Never)."""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.core.field_visibility import register_portal_schema

_Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


@register_portal_schema("client_user")
class ClientExtensionSummary(BaseModel):
    id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    placement_human_id: str
    worker_name: str | None
    role_title: str | None
    current_end_date: date
    new_end_date: date
    status: str
    client_response_at: datetime | None
    client_comment: str | None


@register_portal_schema("client_user")
class ClientOffboardingSummary(BaseModel):
    id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    placement_human_id: str
    worker_name: str | None
    role_title: str | None
    reason: str
    last_working_day: date
    status: str
    client_confirmed_at: datetime | None


class ExtensionRespondIn(_In):
    decision: Literal["confirm", "decline"]
    comment: _Text | None = None


class OffboardingConfirmIn(_In):
    last_working_day_ack: bool
    comment: _Text | None = None


@register_portal_schema("client_user")
class PublicExtensionView(BaseModel):
    tenant_name: str | None
    worker_name: str | None
    role_title: str | None
    current_end_date: date
    new_end_date: date
    status: str
    expires_at: datetime


@register_portal_schema("client_user")
class PublicOffboardingView(BaseModel):
    tenant_name: str | None
    worker_name: str | None
    role_title: str | None
    last_working_day: date
    status: str
    client_confirmed: bool
    expires_at: datetime


@register_portal_schema("client_user")
class PublicActionResult(BaseModel):
    status: str
