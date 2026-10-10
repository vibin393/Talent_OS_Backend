# ruff: noqa: E501
"""Portal Copilot DTOs (Epic 77, spec A4 / A5). Message content is returned only to the owning portal principal; the
admin usage read model carries metadata only (counts, tokens, cost, ratings)."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PortalKind = Literal["client", "vendor", "candidate"]
UnavailableReason = Literal[
    "not_enabled",
    "agentic_off",
    "kill_switch",
    "budget_exhausted",
    "consent_required",
    "no_permission",
    "principal_inactive",
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortalCopilotStatusOut(BaseModel):
    portal: PortalKind
    available: bool
    reason: UnavailableReason | None = None
    tenant_name: str
    questions_per_hour: int
    questions_left_this_hour: int
    retention_days: int
    disclosure: str
    disclosure_extra: str | None = None
    consent_notice_id: uuid.UUID | None = None
    consent_notice_text: str | None = None


class PortalConversationIn(_In):
    pass


class PortalConversationOut(BaseModel):
    id: uuid.UUID
    agent_key: str
    title: str | None
    status: str
    message_count: int
    last_message_at: datetime | None
    created_at: datetime
    expires_at: datetime


class PortalMessageOut(BaseModel):
    id: uuid.UUID
    seq: int
    role: str
    content: dict[str, Any]
    run_id: uuid.UUID | None
    created_at: datetime


class PortalConversationDetailOut(PortalConversationOut):
    messages: list[PortalMessageOut]


class PortalMessageIn(_In):
    text: str = Field(min_length=1, max_length=4000)


class PortalTurnOut(BaseModel):
    message: PortalMessageOut
    run_id: uuid.UUID | None
    stream_url: str | None
    status: str
    reason: str | None = None


class PortalFeedbackIn(_In):
    rating: Literal["up", "down"]
    reason: Literal["wrong", "not_grounded", "unhelpful", "unsafe", "other"] | None = None
    comment: str | None = Field(default=None, max_length=500)


class DraftConfirmIn(_In):
    edits: dict[str, Any] | None = None


class DraftResultOut(BaseModel):
    proposal_id: uuid.UUID
    status: str
    error_code: str | None = None
    open_url: str | None = None
    result: dict[str, Any] | None = None


class PortalConsentIn(_In):
    notice_id: uuid.UUID


# ================================================================ admin
class PortalCopilotSettingsOut(BaseModel):
    client_enabled: bool
    vendor_enabled: bool
    candidate_enabled: bool
    questions_per_hour_external: int
    questions_per_hour_candidate: int
    retention_days: int
    handoff_queue_role_key: str | None
    disclosure_extra: str | None
    agentic_enabled: bool
    updated_at: datetime | None = None


class PortalCopilotSettingsIn(_In):
    client_enabled: bool | None = None
    vendor_enabled: bool | None = None
    candidate_enabled: bool | None = None
    questions_per_hour_external: int | None = Field(default=None, ge=5, le=120)
    questions_per_hour_candidate: int | None = Field(default=None, ge=5, le=60)
    retention_days: int | None = Field(default=None, ge=7, le=90)
    handoff_queue_role_key: str | None = Field(default=None, max_length=40)
    disclosure_extra: str | None = Field(default=None, max_length=300)


class PortalUsageRowOut(BaseModel):
    portal: PortalKind
    questions: int
    helpful_pct: float | None
    drafts_confirmed: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class PortalUsageOut(BaseModel):
    date_from: date
    date_to: date
    rows: list[PortalUsageRowOut]


__all__ = [
    "DraftConfirmIn",
    "DraftResultOut",
    "PortalConsentIn",
    "PortalConversationDetailOut",
    "PortalConversationIn",
    "PortalConversationOut",
    "PortalCopilotSettingsIn",
    "PortalCopilotSettingsOut",
    "PortalCopilotStatusOut",
    "PortalFeedbackIn",
    "PortalKind",
    "PortalMessageIn",
    "PortalMessageOut",
    "PortalTurnOut",
    "PortalUsageOut",
    "PortalUsageRowOut",
]
