# ruff: file-ignore[line-too-long]
"""Timesheet flag and nudge-plan DTOs (Epic 63, FR-311 / FR-312). Internal only: no client-portal schema contains
flags."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class FlagOut(_Out):
    id: uuid.UUID
    timesheet_id: uuid.UUID
    placement_id: uuid.UUID
    rule: str
    label: str
    severity: str
    entry_dates: list[date]
    facts: dict[str, Any]
    group_key: str | None
    explanation: str | None
    explanation_source: str
    rules_description: str
    status: str
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    review_note: str | None
    timesheet_version: int
    created_at: datetime


class FlagListRow(FlagOut):
    timesheet_human_id: str | None = None
    client_id: uuid.UUID | None = None


class FlagReviewIn(_In):
    status: Literal["reviewed", "dismissed"]
    note: str | None = Field(default=None, max_length=500)


class FlagExplanationIn(_In):
    flag_id: uuid.UUID
    explanation: str = Field(min_length=1, max_length=600)
    group_key: str | None = Field(default=None, max_length=40, pattern=r"^[a-z0-9_]{1,40}$")


class FlagCount(_Out):
    timesheet_id: uuid.UUID
    open_count: int
    max_severity: str


# ---------------------------------------------------------------- nudges
class NudgeItem(_Out):
    timesheet_id: uuid.UUID
    human_id: str
    kind: Literal["submit", "approve"]
    status: str
    period_start: date
    period_end: date
    age_business_days: int
    nudges_sent: int
    recipient_kind: str | None
    recipient_id: uuid.UUID | None
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    escalate: bool = False
    approver_step: str | None = None


class NudgePlan(_Out):
    account_manager_id: uuid.UUID
    today: date
    submit: list[NudgeItem] = Field(default_factory=list)
    approve: list[NudgeItem] = Field(default_factory=list)
    escalate: list[NudgeItem] = Field(default_factory=list)
    max_reached: list[NudgeItem] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.submit or self.approve or self.escalate or self.max_reached)


__all__ = [
    "FlagCount",
    "FlagExplanationIn",
    "FlagListRow",
    "FlagOut",
    "FlagReviewIn",
    "NudgeItem",
    "NudgePlan",
]
