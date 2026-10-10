"""Bench management API schemas (Epic 24, FR-125..FR-127). Money is a decimal string (AD-12); cost fields are
``None`` with ``cost_masked=true`` for principals without ``compensation:view`` (AD-11)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.core.money import DecimalStr

BenchStatus = Literal["on_bench", "proposed", "redeployed", "released"]
BenchOrigin = Literal["placement_ended", "contractor_opt_in", "manual"]
ReleaseReason = Literal["resigned", "terminated", "contractor_unavailable", "other"]
DayBasis = Literal["calendar", "weekdays"]
ReviewDecision = Literal["reviewed", "dismissed"]


class BenchSettingsOut(BaseModel):
    hours_per_day: DecimalStr
    monthly_rate_divisor: DecimalStr
    bench_day_basis: DayBasis
    upcoming_roll_off_days: int
    notify_strong_bench_match: bool
    require_bench_review_for_vendor_release: bool
    contractor_opt_in_enabled: bool


class BenchSettingsUpdate(BaseModel):
    hours_per_day: Decimal | None = Field(default=None, gt=0, le=24, decimal_places=2)
    monthly_rate_divisor: Decimal | None = Field(default=None, gt=0, le=31, decimal_places=2)
    bench_day_basis: DayBasis | None = None
    upcoming_roll_off_days: int | None = Field(default=None, ge=1, le=365)
    notify_strong_bench_match: bool | None = None
    require_bench_review_for_vendor_release: bool | None = None
    contractor_opt_in_enabled: bool | None = None


class SkillRef(BaseModel):
    skill_id: uuid.UUID | None = None
    tenant_skill_id: uuid.UUID | None = None
    name: str


class BenchEntryOut(BaseModel):
    id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None = None
    candidate_human_id: str | None = None
    source_placement_id: uuid.UUID | None
    legal_entity_id: uuid.UUID | None
    status: BenchStatus
    origin: BenchOrigin
    available_from: date
    skills: list[SkillRef]
    location: dict[str, Any] | None
    work_authorization: str | None
    preferred_locations: list[Any]
    bench_days: int
    cost_per_day: DecimalStr | None
    cost_to_date: DecimalStr | None
    cost_currency: str | None
    cost_source: str | None
    cost_rate_unit_basis: str | None
    cost_unknown: bool
    cost_masked: bool
    proposed_application_ids: list[uuid.UUID]
    proposed_count: int
    redeployed_placement_id: uuid.UUID | None
    released_reason: ReleaseReason | None
    released_note: str | None
    closed_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class BenchEntryPage(BaseModel):
    items: list[BenchEntryOut]
    total: int
    page: int
    page_size: int


class BenchEntryCreate(BaseModel):
    candidate_id: uuid.UUID
    available_from: date
    source_placement_id: uuid.UUID | None = None
    legal_entity_id: uuid.UUID | None = None
    work_authorization: str | None = Field(default=None, max_length=500)
    location: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=5000)


class BenchEntryUpdate(BaseModel):
    notes: str | None = Field(default=None, max_length=5000)
    location: dict[str, Any] | None = None
    work_authorization: str | None = Field(default=None, max_length=500)
    available_from: date | None = None
    preferred_locations: list[str] | None = None
    refresh_skills: bool = False


class CostOverrideIn(BaseModel):
    cost_per_day: Decimal = Field(ge=0, max_digits=16, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)


class ReleaseIn(BaseModel):
    reason: ReleaseReason
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _note_for_other(self) -> "ReleaseIn":
        if self.reason == "other" and not (self.note or "").strip():
            msg = "a note is required when the reason is 'other'"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------- matching
class BenchMatchCandidate(BaseModel):
    id: uuid.UUID
    human_id: str | None = None
    display_name: str | None = None
    current_title: str | None = None
    city: str | None = None


class BenchMatchItem(BaseModel):
    candidate: BenchMatchCandidate
    pool: Literal["bench", "rolling_off"]
    match_id: uuid.UUID | None
    overall: float | None
    sub_scores: dict[str, int | None]
    explanation: str | None
    dismissed_at: datetime | None
    application_id: uuid.UUID | None
    score_pending: bool
    score_unavailable_reason: Literal["no_consent"] | None
    strong: bool
    bench_entry_id: uuid.UUID | None
    bench_status: BenchStatus | None
    bench_days: int | None
    available_from: date | None
    rolling_off_end_date: date | None
    cost_per_day: DecimalStr | None
    cost_currency: str | None
    cost_masked: bool
    review_decision: ReviewDecision | None
    review_reason: str | None


class BenchMatchesOut(BaseModel):
    demand_id: uuid.UUID
    strong_threshold: int
    items: list[BenchMatchItem]
    unreviewed_strong: int
    reviewed_complete: bool
    completed_at: datetime | None


class ReviewIn(BaseModel):
    candidate_id: uuid.UUID
    decision: ReviewDecision
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reason_for_dismiss(self) -> "ReviewIn":
        if self.decision == "dismissed" and not (self.reason or "").strip():
            msg = "a reason is required to dismiss a bench match"
            raise ValueError(msg)
        return self


class ReviewOut(BaseModel):
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    decision: ReviewDecision
    reason: str | None
    decided_by: uuid.UUID | None
    decided_at: datetime


class ReviewCompleteOut(BaseModel):
    demand_id: uuid.UUID
    completed_at: datetime
    completed_by: uuid.UUID | None


# ---------------------------------------------------------------- reporting
class MoneyByCurrency(BaseModel):
    currency: str
    amount: DecimalStr | None


class MonthCost(BaseModel):
    month: str
    currency: str
    amount: DecimalStr | None


class WorkerBenchDays(BaseModel):
    bench_entry_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None
    status: BenchStatus
    bench_days: int


class RollOffRow(BaseModel):
    placement_id: uuid.UUID
    placement_human_id: str
    candidate_id: uuid.UUID
    candidate_name: str | None
    client_id: uuid.UUID
    client_name: str | None
    role_title: str | None
    end_date: date
    days_left: int


class BenchSummaryOut(BaseModel):
    as_of: date
    count_on_bench: int
    avg_bench_days: DecimalStr | None
    bench_days_by_worker: list[WorkerBenchDays]
    total_cost_to_date: list[MoneyByCurrency]
    cost_by_month: list[MonthCost]
    upcoming_roll_offs_30: int
    upcoming_roll_offs_60: int
    redeployment_rate: DecimalStr | None
    redeployed: int
    released: int
    masked: bool
    filters: dict[str, dict[str, Any]]
