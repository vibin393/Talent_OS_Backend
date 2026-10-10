"""Compliance chasing and BGV follow-ups (Epic 62 A3 / A8; internal principals only)."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SuppressedChannel(BaseModel):
    channel: str
    reason: str


class ChaseOut(BaseModel):
    id: uuid.UUID
    candidate_id: uuid.UUID
    placement_id: uuid.UUID | None
    item_kind: Literal["checklist_item", "credential", "permit"]
    item_ref: uuid.UUID
    item_label: str
    due_basis: Literal["start_date", "expiry"]
    due_on: date
    offset_days: int
    channels: list[str]
    suppressed: list[SuppressedChannel]
    escalated: bool
    task_id: uuid.UUID | None
    escalated_to_name: str | None = None
    agent_run_id: uuid.UUID | None
    sent_at: datetime


class RiskItem(BaseModel):
    label: str
    status: str
    kind: str


class RiskRow(BaseModel):
    placement_id: uuid.UUID
    placement_ref: str | None
    candidate_id: uuid.UUID
    worker_name: str
    client_id: uuid.UUID
    client_name: str | None
    start_date: date
    days_to_start: int
    status: str
    missing_items: list[RiskItem]
    last_contact: datetime | None
    portal_last_seen: datetime | None
    chases_sent: int
    escalation_task_id: uuid.UUID | None
    escalated: bool
    bgv_status: str | None


class RiskListOut(BaseModel):
    days: int
    rows: list[RiskRow]
    cadence: dict[str, Any] = Field(default_factory=dict)


class FollowUpOut(BaseModel):
    id: uuid.UUID
    check_id: uuid.UUID
    check_type: str
    check_label: str
    provider_name: str | None
    channel: Literal["connector", "email"]
    seq: int
    status: str
    provider_status: str | None
    days_overdue: int | None
    sent_at: datetime


class OverdueCheckOut(BaseModel):
    check_id: uuid.UUID
    type: str
    subject_label: str
    provider_name: str | None
    provider_kind: str | None
    has_connector: bool
    due_on: date | None
    days_overdue: int
    follow_ups_sent: int
    last_follow_up_on: date | None
    next_due_on: date | None
    limit_reached: bool


class CaseFollowUpsOut(BaseModel):
    case_id: uuid.UUID
    max_follow_ups: int
    follow_up_every_working_days: int
    overdue: list[OverdueCheckOut]
    follow_ups: list[FollowUpOut]
    limit_task_owner_name: str | None = None


__all__ = [
    "CaseFollowUpsOut",
    "ChaseOut",
    "FollowUpOut",
    "OverdueCheckOut",
    "RiskItem",
    "RiskListOut",
    "RiskRow",
    "SuppressedChannel",
]
