"""Client Portal schemas of the pipeline domain (Epic 18, FR-101): sent submissions, comparison, feedback and
interview slots. Client-only classes (FR-80): never pay rate, burden, margin, match score, source / vendor,
candidate email / phone or internal notes. The recruiter's submission note is exposed as ``recruiter_summary``
(the text the client already received in the submission email)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.field_visibility import register_portal_schema
from app.core.money import DecimalStr


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@register_portal_schema("client_user")
class ClientFeedbackSnapshot(BaseModel):
    decision: str
    comment: str | None
    received_at: datetime


class SlotIn(_In):
    starts_at: datetime
    ends_at: datetime
    tz: str = Field(default="Asia/Kolkata", max_length=64)

    @model_validator(mode="after")
    def _order(self) -> "SlotIn":
        if self.ends_at <= self.starts_at:
            msg = "ends_at must be after starts_at"
            raise ValueError(msg)
        return self


@register_portal_schema("client_user")
class SlotOut(BaseModel):
    starts_at: datetime
    ends_at: datetime
    tz: str


@register_portal_schema("client_user")
class ClientSlotProposalOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    slots: list[SlotOut]
    chosen_index: int | None
    created_at: datetime


@register_portal_schema("client_user")
class ClientSubmissionOut(BaseModel):
    id: uuid.UUID
    human_id: str
    demand_id: uuid.UUID
    demand_title: str | None
    candidate_display_name: str
    headline: str | None
    experience_months: int | None
    availability_days: int | None
    bill_rate: DecimalStr
    currency: str
    rate_unit: str
    recruiter_summary: str | None
    has_resume: bool
    sent_at: datetime | None
    feedback_status: str
    latest_feedback: ClientFeedbackSnapshot | None
    interview_status: str | None
    slot_proposals: list[ClientSlotProposalOut] = Field(default_factory=list)


@register_portal_schema("client_user")
class ClientCompareOut(BaseModel):
    demand_id: uuid.UUID
    items: list[ClientSubmissionOut]


class ClientCompareIn(_In):
    submission_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)


class ClientFeedbackIn(_In):
    decision: Literal["interested", "rejected", "on_hold"]
    comment: str | None = Field(default=None, max_length=4000)


class ClientSlotProposalIn(_In):
    slots: list[SlotIn] = Field(min_length=1, max_length=5)


class ClientChooseSlotIn(_In):
    index: int = Field(ge=0, le=4)
