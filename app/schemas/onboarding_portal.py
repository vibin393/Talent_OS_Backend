"""Worker "My Work" onboarding schemas (Epic 23 A7). Never: internal tasks, owner users, decided-by identity,
the template snapshot or compliance source details. Bank / PAN values are masked (last 4)."""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.field_visibility import register_portal_schema


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


@register_portal_schema("worker")
class PortalTaskOut(BaseModel):
    id: uuid.UUID
    kind: str
    label: str
    instructions: str | None
    mandatory: bool
    status: str
    due_on: date | None
    return_comment: str | None
    submitted_at: datetime | None
    draft: dict[str, Any] | None
    masked: dict[str, str | None] = Field(default_factory=dict)
    form: dict[str, Any] | None = None  # client_form schema / tax declaration mode / document category
    signature_request_id: uuid.UUID | None = None
    signer_id: uuid.UUID | None = None


@register_portal_schema("worker")
class PortalCaseSummary(BaseModel):
    id: uuid.UUID
    human_id: str | None
    status: str
    progress_pct: int
    planned_start_date: date | None
    open_tasks: int


@register_portal_schema("worker")
class PortalCaseOut(PortalCaseSummary):
    tasks: list[PortalTaskOut]


class DraftIn(_In):
    draft: dict[str, Any]


class SubmitIn(_In):
    payload: dict[str, Any] = Field(default_factory=dict)


class AcknowledgeIn(_In):
    accepted: bool


@register_portal_schema("worker")
class PortalSignatureRefOut(BaseModel):
    signature_request_id: uuid.UUID | None
    signer_id: uuid.UUID | None
    status: str
