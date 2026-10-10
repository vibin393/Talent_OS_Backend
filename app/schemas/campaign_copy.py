# ruff: noqa: E501
"""AI campaign copy DTOs (Epic 78, FR-401..FR-403). The agent result model ``CampaignCopyResult`` is the
``emit_result`` schema of the ``campaign_copy`` agent (``extra="forbid"``)."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Channel = Literal["email", "sms", "whatsapp"]
Purpose = Literal["reengage", "new_jobs", "event", "referral_ask", "nurture", "other"]
Tone = Literal["friendly", "professional", "concise", "enthusiastic"]
ApplyMode = Literal["single", "ab_subjects", "per_segment_campaigns"]
# The P3 segment semantic fields a segment may be described by (aggregate labels, never person ids).
ATTRIBUTE_FIELDS = ("city", "primary_skill", "experience_band", "last_active_band")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CopyBrief(_In):
    purpose: Purpose
    purpose_note: str = Field(default="", max_length=300)
    tone: Tone = "friendly"
    call_to_action: str = Field(default="", max_length=80)
    posting_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    demand_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    language: Literal["en"] = "en"


class AudienceSourceRef(_In):
    kind: Literal["audience_source"]
    id: uuid.UUID


class AttributeRef(_In):
    kind: Literal["attribute"]
    field: Literal["city", "primary_skill", "experience_band", "last_active_band"]
    value: str = Field(min_length=1, max_length=80)


class CopySegment(_In):
    key: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    label: str = Field(min_length=1, max_length=120)
    source: Annotated[AudienceSourceRef | AttributeRef, Field(discriminator="kind")]


class CopyRequestIn(_In):
    campaign_id: uuid.UUID | None = None
    sequence_step_id: uuid.UUID | None = None
    channel: Channel
    brief: CopyBrief
    # Limits are enforced by the service (422 ``copy_request_too_large`` with the maxima), not by the schema.
    segments: list[CopySegment] = Field(min_length=1)
    variants_per_segment: int = Field(ge=1)
    channel_template_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_target(self) -> "CopyRequestIn":
        if (self.campaign_id is None) == (self.sequence_step_id is None):
            msg = "Give exactly one of campaign_id or sequence_step_id"
            raise ValueError(msg)
        keys = [s.key for s in self.segments]
        if len(set(keys)) != len(keys):
            msg = "Segment keys must be unique"
            raise ValueError(msg)
        return self


class Finding(BaseModel):
    code: str
    severity: Literal["block", "warn"]
    field: str
    excerpt: str = ""


class JobLinkBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posting_id: uuid.UUID
    cta: str = Field(min_length=1, max_length=80)


class CopyDraftOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    segment_key: str
    segment_label: str
    variant_no: int
    channel: Channel
    subject: str | None
    preheader: str | None
    body_html: str | None
    template_values: dict[str, str] | None
    job_link_blocks: list[dict[str, Any]]
    status: str
    findings: list[Finding]
    blocked: bool
    needs_ack: bool
    warnings_acknowledged: bool
    ai_generated: bool
    approved_by: uuid.UUID | None
    approved_by_name: str | None = None
    approved_at: datetime | None
    rubber_stamp: bool
    edited_at: datetime | None
    used_in_campaign_id: uuid.UUID | None
    used_at: datetime | None
    content_sha256: str


class SegmentOut(BaseModel):
    key: str
    label: str
    source: dict[str, Any]
    reachable: int | None
    drafts: list[CopyDraftOut]


class CopyRequestOut(BaseModel):
    id: uuid.UUID
    campaign_id: uuid.UUID | None
    sequence_id: uuid.UUID | None
    sequence_step_id: uuid.UUID | None
    step_position: int | None
    channel: Channel
    status: str
    variants_per_segment: int
    segment_count: int
    agent_run_id: uuid.UUID | None
    error_code: str | None
    fallback_message: str | None = None
    requested_by: uuid.UUID
    created_at: datetime


class CopyRequestDetailOut(CopyRequestOut):
    brief: dict[str, Any]
    segments: list[SegmentOut]
    summary: dict[str, int]


class DraftEditIn(_In):
    subject: str | None = Field(default=None, max_length=200)
    preheader: str | None = Field(default=None, max_length=200)
    body_html: str | None = Field(default=None, max_length=51_200)
    template_values: dict[str, str] | None = None
    job_link_blocks: list[JobLinkBlock] | None = None


class ApproveIn(_In):
    note: str | None = Field(default=None, max_length=500)


class ApplyIn(_In):
    draft_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    mode: ApplyMode

    @field_validator("draft_ids")
    @classmethod
    def _unique(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(dict.fromkeys(v))


class ApplyOut(BaseModel):
    mode: ApplyMode
    campaign_ids: list[uuid.UUID]
    sequence_id: uuid.UUID | None = None
    draft_ids: list[uuid.UUID]


class CopyStatusOut(BaseModel):
    """The campaign editor banner (B3): the AI copy in use on a Campaign and whether it changed after approval."""

    uses_ai_copy: bool
    draft_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    approved_by: uuid.UUID | None = None
    approved_by_name: str | None = None
    approved_at: datetime | None = None
    changed_after_approval: bool = False


# ---------------------------------------------------------------- agent result (emit_result schema)
class ResultJobLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posting_id: uuid.UUID
    cta: str = Field(min_length=1, max_length=80)


class ResultVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str | None = Field(default=None, max_length=200)
    preheader: str | None = Field(default=None, max_length=200)
    body_html: str | None = Field(default=None, max_length=51_200)
    template_values: dict[str, str] | None = None
    job_link_blocks: list[ResultJobLink] = Field(default_factory=list, max_length=10)


class ResultSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    variants: list[ResultVariant] = Field(min_length=1, max_length=3)


class CampaignCopyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[ResultSegment] = Field(min_length=1, max_length=20)


__all__ = [
    "ATTRIBUTE_FIELDS",
    "ApplyIn",
    "ApplyOut",
    "ApproveIn",
    "CampaignCopyResult",
    "CopyBrief",
    "CopyDraftOut",
    "CopyRequestDetailOut",
    "CopyRequestIn",
    "CopyRequestOut",
    "CopySegment",
    "CopyStatusOut",
    "DraftEditIn",
    "Finding",
    "SegmentOut",
]
