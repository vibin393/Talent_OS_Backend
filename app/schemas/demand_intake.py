# ruff: noqa: E501
"""Demand intake DTOs (Epic 55, story 55.1 / 55.2). Hand-mirrored in ``Frontend/src/features/agents/demand/types.ts``."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

IntakeMode = Literal["off", "connected_mailbox", "platform_address"]
IntakeSource = Literal["platform_address", "connected_mailbox", "upload", "paste"]
IntakeStatus = Literal[
    "received", "blocked", "unmatched", "ambiguous", "matched", "processing", "drafted", "no_roles", "failed",
    "discarded",
]  # fmt: skip
MatchMethod = Literal["contact_email", "verified_domain", "manual", "forwarded_contact"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ settings
class IntakeSettingsOut(BaseModel):
    intake_mode: IntakeMode
    mailbox_user_id: uuid.UUID | None
    mailbox_folder: str
    mailbox_status: Literal["connected", "needs_reconnect", "not_connected"] | None
    fallback_owner_id: uuid.UUID | None
    accept_forwarded: bool
    require_sender_auth: bool
    platform_address: str | None
    warnings: list[str] = []


class IntakeSettingsIn(_In):
    intake_mode: IntakeMode | None = None
    mailbox_user_id: uuid.UUID | None = None
    mailbox_folder: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None
    ) = None
    fallback_owner_id: uuid.UUID | None = None
    accept_forwarded: bool | None = None
    require_sender_auth: bool | None = None


class SenderDomainIn(_In):
    domain: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=253)]
    client_id: uuid.UUID
    confirmed: bool
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None


class SenderDomainOut(BaseModel):
    id: uuid.UUID
    domain: str
    client_id: uuid.UUID
    client_name: str | None
    verified_by: uuid.UUID
    verified_by_name: str | None
    verified_at: datetime
    note: str | None


# ================================================================ messages
class AttachmentOut(BaseModel):
    file_id: uuid.UUID | None
    filename: str
    media_type: str
    size: int
    scan_status: str
    text_file_id: uuid.UUID | None = None


class MatchOut(BaseModel):
    status: str
    method: MatchMethod | None
    client_id: uuid.UUID | None
    client_name: str | None = None
    contact_id: uuid.UUID | None
    contact_name: str | None = None
    owner_user_id: uuid.UUID | None
    explanation: str | None = None


class IntakeMessageSummaryOut(BaseModel):
    id: uuid.UUID
    source: IntakeSource
    from_address: str | None
    from_name: str | None
    subject: str | None
    received_at: datetime
    status: IntakeStatus
    status_reason: str | None
    client_id: uuid.UUID | None
    client_name: str | None
    draft_demand_ids: list[uuid.UUID]
    group_label: str | None


class DraftRef(BaseModel):
    id: uuid.UUID
    human_id: str | None
    title: str | None
    status: str | None


class IntakeMessageOut(IntakeMessageSummaryOut):
    body_preview: str | None
    attachments: list[AttachmentOut]
    match: MatchOut
    drafts: list[DraftRef]
    owner_user_id: uuid.UUID | None
    run_id: uuid.UUID | None
    task_id: uuid.UUID | None
    processed_at: datetime | None
    auth_result: str | None


class IntakeQuery(BaseModel):
    status: list[IntakeStatus] | None = None
    tab: Literal["needs_attention", "drafted", "all"] | None = None
    client_id: uuid.UUID | None = None


class AssignClientIn(_In):
    client_id: uuid.UUID
    contact_id: uuid.UUID | None = None


class DiscardIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


class IntakeReceiptOut(BaseModel):
    message_id: uuid.UUID
    status: str
    duplicate: bool = False


# ================================================================ agent reads
class AgentAttachmentText(BaseModel):
    filename: str
    media_type: str
    text: str


class IntakeForAgentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    source: str
    subject: str | None
    received_at: datetime
    match: MatchOut
    body_text: str
    attachments: list[AgentAttachmentText]
    # ``ack_contact_on_intake``: draft a short acknowledgement to the matched contact (always a Proposal).
    acknowledge_contact: bool = False


# ================================================================ extraction (fast tier, schema-bound)
QuoteStr = Annotated[str, StringConstraints(max_length=1000)]


class QuotedText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    source_quote: QuoteStr | None = None


class QuotedNumber(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    source_quote: QuoteStr | None = None


class QuotedInt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int | None = None
    source_quote: QuoteStr | None = None


class QuotedList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: list[str] = Field(default_factory=list)
    source_quote: QuoteStr | None = None


class RoleExtraction(BaseModel):
    """One requested role: every P1 ``IntakeExtraction`` field as ``{value, source_quote}``."""

    model_config = ConfigDict(extra="forbid")

    role_index: int = 0
    title: QuotedText | None = None
    mandatory_skills: QuotedList | None = None
    preferred_skills: QuotedList | None = None
    min_experience_years: QuotedNumber | None = None
    max_experience_years: QuotedNumber | None = None
    positions: QuotedInt | None = None
    location: QuotedText | None = None
    country: QuotedText | None = None
    work_mode: QuotedText | None = None
    bill_rate: QuotedNumber | None = None
    pay_rate_min: QuotedNumber | None = None
    pay_rate_max: QuotedNumber | None = None
    currency: QuotedText | None = None
    rate_unit: QuotedText | None = None
    start_date: QuotedText | None = None
    employment_type: QuotedText | None = None
    certifications: QuotedList | None = None


class IntakeRolesExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: list[RoleExtraction] = Field(default_factory=list, max_length=5)
    client_hints: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list, max_length=5
    )

    @field_validator("roles", mode="before")
    @classmethod
    def _cap_roles(cls, value: Any) -> Any:
        return value[:5] if isinstance(value, list) else value

    @field_validator("client_hints", mode="before")
    @classmethod
    def _cap_hints(cls, value: Any) -> Any:
        return [str(v)[:200] for v in value[:5]] if isinstance(value, list) else value


class ExtractedField(BaseModel):
    value: Any
    source_quote: str


class ExtractedRole(BaseModel):
    role_index: int
    title: str | None
    positions: int | None
    fields: dict[str, ExtractedField]
    mandatory_skills: list[str]
    preferred_skills: list[str]


class ExtractionOut(BaseModel):
    roles: list[ExtractedRole]
    client_hints: list[str]


# ================================================================ agent settings (thresholds, JD template, scan)
class AgentSettingsOut(BaseModel):
    duplicate_threshold: Decimal
    duplicate_window_days: int
    sla_risk_probability: Decimal
    sla_digest_max_items: int
    roles_per_message_max: int
    sla_scan_owner_id: uuid.UUID | None
    ack_contact_on_intake: bool
    jd_template_md: str | None
    warnings: list[str] = []


class AgentSettingsIn(_In):
    duplicate_threshold: Annotated[Decimal, Field(ge=Decimal("0.50"), le=Decimal("0.95"))] | None = None
    duplicate_window_days: Annotated[int, Field(ge=7, le=365)] | None = None
    sla_risk_probability: Annotated[Decimal, Field(ge=Decimal("0.30"), le=Decimal("0.95"))] | None = None
    sla_digest_max_items: Annotated[int, Field(ge=5, le=100)] | None = None
    roles_per_message_max: Annotated[int, Field(ge=1, le=5)] | None = None
    sla_scan_owner_id: uuid.UUID | None = None
    ack_contact_on_intake: bool | None = None
    jd_template_md: Annotated[str, StringConstraints(max_length=10_000)] | None = None
