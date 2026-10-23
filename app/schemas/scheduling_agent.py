# ruff: file-ignore[line-too-long]
"""Scheduling agent contract DTOs (Epic 53 contract layer, spec 0.12; filled by Epic 59). Frozen; field sets pinned
by ``tests/contracts/signatures.json``."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SlotProposalIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    application_id: uuid.UUID
    interview_stage_id: uuid.UUID | None = None
    interview_id: uuid.UUID | None = None
    duration_minutes: int
    buffer_minutes: int = 0
    participant_user_ids: tuple[uuid.UUID, ...]
    candidate_timezone: str
    max_slots: int = 5
    horizon_business_days: int = 10


class Slot(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    all_known_free: bool
    unknown_participants: tuple[uuid.UUID, ...] = ()


class SlotProposalOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_ref: uuid.UUID
    slots: tuple[Slot, ...]
    unknown_participants: tuple[uuid.UUID, ...]
    partial: bool


# ================================================================ Epic 59 (additive: the API and agent DTOs)
from typing import Annotated, Any, Literal  # ruff: ignore[module-import-not-at-top-of-file]

from pydantic import (  # ruff: ignore[module-import-not-at-top-of-file]
    Field,
    StringConstraints,
    field_validator,
)

RequestStatus = Literal[
    "open", "slots_offered", "picked", "booked", "counter_proposed", "handed_off", "cancelled", "expired"
]
InterviewMode = Literal["video", "phone", "in_person"]
OfferChannel = Literal["email", "whatsapp", "sms", "portal"]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")]
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeWindow(_In):
    start: datetime
    end: datetime


class RequestStartIn(_In):
    stage_id: uuid.UUID
    participant_user_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=10)] | None = None
    duration_minutes: Annotated[int, Field(ge=15, le=480)] | None = None
    mode: InterviewMode | None = None
    candidate_timezone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None = None


class RescheduleRequestIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class CancelRequestIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None


class OfferSlotOut(BaseModel):
    index: int
    start: datetime
    end: datetime
    all_known_free: bool
    unknown_participants: list[uuid.UUID] = []


class OfferOut(BaseModel):
    id: uuid.UUID
    round: int
    status: str
    partial: bool
    channel: str | None
    slots: list[OfferSlotOut]
    unknown_participants: list[uuid.UUID]
    sent_at: datetime | None
    expires_at: datetime | None
    picked_index: int | None
    picked_at: datetime | None
    counter_windows: list[dict[str, Any]] | None
    candidate_note: str | None


class HistoryOut(BaseModel):
    id: uuid.UUID
    kind: str
    actor_kind: str
    actor_id: uuid.UUID | None
    detail: dict[str, Any]
    at: datetime


class ParticipantRef(BaseModel):
    user_id: uuid.UUID
    name: str | None
    timezone: str
    calendar_connected: bool


class RequestOut(BaseModel):
    id: uuid.UUID
    human_id: str
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    demand_id: uuid.UUID
    stage_id: uuid.UUID
    stage_name: str | None
    interview_id: uuid.UUID | None
    kind: str
    status: RequestStatus
    round: int
    max_rounds: int
    duration_minutes: int
    buffer_minutes: int
    participant_user_ids: list[uuid.UUID]
    mode: str | None
    candidate_timezone: str
    owner_user_id: uuid.UUID
    current_run_id: uuid.UUID | None
    booked_interview_id: uuid.UUID | None
    handoff_reason: str | None
    reschedule_reason: str | None
    created_at: datetime
    updated_at: datetime


class RequestDetailOut(RequestOut):
    participants: list[ParticipantRef]
    offers: list[OfferOut]
    history: list[HistoryOut]


class RequestOptionsOut(BaseModel):
    """Prefill of the "Let the agent schedule" dialog."""

    agent_on: bool
    stage_id: uuid.UUID
    stage_name: str
    duration_minutes: int
    candidate_timezone: str
    timezone_source: str
    open_request_id: uuid.UUID | None


class WorkingHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mon: list[tuple[HHMM, HHMM]] = []
    tue: list[tuple[HHMM, HHMM]] = []
    wed: list[tuple[HHMM, HHMM]] = []
    thu: list[tuple[HHMM, HHMM]] = []
    fri: list[tuple[HHMM, HHMM]] = []
    sat: list[tuple[HHMM, HHMM]] = []
    sun: list[tuple[HHMM, HHMM]] = []

    @field_validator("*")
    @classmethod
    def _ordered(cls, v: list[tuple[str, str]]) -> list[tuple[str, str]]:
        if len(v) > 4:
            msg = "at most 4 intervals per day"
            raise ValueError(msg)
        for start, end in v:
            if start >= end:
                msg = "an interval must end after it starts"
                raise ValueError(msg)
        return v


class SchedulingPrefsIn(_In):
    timezone: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    working_hours: WorkingHours
    holiday_country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None
    holiday_region: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)] | None = None
    max_interviews_per_day: Annotated[int, Field(ge=1, le=20)] | None = None
    buffer_minutes: Annotated[int, Field(ge=0, le=120)] | None = None


class SchedulingPrefsOut(BaseModel):
    user_id: uuid.UUID
    timezone: str
    working_hours: dict[str, Any]
    holiday_country: str | None
    holiday_region: str | None
    max_interviews_per_day: int | None
    buffer_minutes: int | None
    is_default: bool
    calendar_connected: bool


class CandidateHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: HHMM = "08:00"
    end: HHMM = "21:00"


class SchedulingSettingsIn(_In):
    max_slots: Annotated[int, Field(ge=1, le=10)] | None = None
    horizon_business_days: Annotated[int, Field(ge=3, le=20)] | None = None
    slot_step_minutes: Literal[15, 30, 60] | None = None
    default_buffer_minutes: Annotated[int, Field(ge=0, le=60)] | None = None
    candidate_hours: CandidateHours | None = None
    max_rounds: Annotated[int, Field(ge=1, le=5)] | None = None
    link_days: Annotated[int, Field(ge=1, le=14)] | None = None
    reminder_offsets_minutes: (
        Annotated[list[Annotated[int, Field(ge=10, le=10080)]], Field(max_length=4)] | None
    ) = None
    reminder_channels: Annotated[list[Literal["email", "whatsapp"]], Field(max_length=2)] | None = None
    scorecard_nudge_after_hours: Annotated[int, Field(ge=2, le=72)] | None = None
    scorecard_nudge_max: Annotated[int, Field(ge=0, le=5)] | None = None
    default_working_hours: WorkingHours | None = None
    whatsapp_template_id: uuid.UUID | None = None


class SchedulingSettingsOut(BaseModel):
    max_slots: int
    horizon_business_days: int
    slot_step_minutes: int
    default_buffer_minutes: int
    candidate_hours: dict[str, Any]
    max_rounds: int
    link_days: int
    reminder_offsets_minutes: list[int]
    reminder_channels: list[str]
    scorecard_nudge_after_hours: int
    scorecard_nudge_max: int
    default_working_hours: dict[str, Any]
    whatsapp_template_id: uuid.UUID | None
    agent_on: bool


class SchedulingResult(BaseModel):
    """``emit_result`` schema of the Scheduling Agent."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "waiting", "booked", "handed_off", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=2000)
    request_status: str | None = None
    interview_id: uuid.UUID | None = None


__all__ = [
    "CancelRequestIn",
    "HistoryOut",
    "OfferOut",
    "OfferSlotOut",
    "RequestDetailOut",
    "RequestOptionsOut",
    "RequestOut",
    "RequestStartIn",
    "RescheduleRequestIn",
    "SchedulingPrefsIn",
    "SchedulingPrefsOut",
    "SchedulingResult",
    "SchedulingSettingsIn",
    "SchedulingSettingsOut",
    "Slot",
    "SlotProposalIn",
    "SlotProposalOut",
    "TimeWindow",
    "WorkingHours",
]
