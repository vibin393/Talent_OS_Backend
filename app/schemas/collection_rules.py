# ruff: noqa: E501
"""Collection rules, holds, plan and log DTOs (Epic 63, FR-314 / FR-315). Internal only."""

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Tone = Literal["courteous", "friendly", "firm", "escalation"]
Audience = Literal["billing_contacts", "escalation_contact", "both"]
AUTO_RULE_MESSAGE = (
    "Only pre-due courteous or friendly reminders to billing contacts with a template can be sent automatically."
)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StepIn(_In):
    position: int = Field(ge=1, le=20)
    offset_days: int = Field(ge=-30, le=180)
    tone: Tone
    audience: Audience = "billing_contacts"
    email_template_id: uuid.UUID | None = None
    allow_auto: bool = False
    create_task: bool = False
    active: bool = True

    @model_validator(mode="after")
    def _auto(self) -> "StepIn":
        if self.allow_auto and not (
            self.offset_days < 0
            and self.email_template_id is not None
            and self.audience == "billing_contacts"
            and self.tone in ("courteous", "friendly")
        ):
            raise ValueError(AUTO_RULE_MESSAGE)
        return self


class StepOut(_Out):
    id: uuid.UUID
    position: int
    offset_days: int
    tone: str
    audience: str
    email_template_id: uuid.UUID | None
    allow_auto: bool
    create_task: bool
    active: bool


class RuleIn(_In):
    client_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    active: bool = True
    escalation_contact_id: uuid.UUID | None = None
    max_reminders_per_week: int = Field(default=2, ge=1, le=7)
    quiet_start: time = time(18, 0)
    quiet_end: time = time(9, 0)
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    steps: list[StepIn] | None = Field(default=None, max_length=20)


class RuleUpdate(_In):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None
    escalation_contact_id: uuid.UUID | None = None
    max_reminders_per_week: int | None = Field(default=None, ge=1, le=7)
    quiet_start: time | None = None
    quiet_end: time | None = None
    timezone: str | None = Field(default=None, max_length=64)


class StepsIn(_In):
    steps: list[StepIn] = Field(max_length=20)


class RuleOut(_Out):
    id: uuid.UUID
    client_id: uuid.UUID | None
    client_name: str | None = None
    name: str
    active: bool
    escalation_contact_id: uuid.UUID | None
    escalation_contact_name: str | None = None
    max_reminders_per_week: int
    quiet_start: time
    quiet_end: time
    timezone: str
    steps: list[StepOut] = Field(default_factory=list)
    auto_steps: int = 0
    updated_at: datetime | None = None


# ---------------------------------------------------------------- holds
class HoldIn(_In):
    client_id: uuid.UUID
    document_kind: Literal["invoice", "debit_note"] | None = None
    document_id: uuid.UUID | None = None
    mode: Literal["paused", "takeover"] = "paused"
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _doc(self) -> "HoldIn":
        if (self.document_id is None) != (self.document_kind is None):
            raise ValueError("Give both document kind and id, or neither")
        if self.mode == "takeover" and self.document_id is None:
            raise ValueError("A takeover is for one invoice thread")
        return self


class HoldOut(_Out):
    id: uuid.UUID
    client_id: uuid.UUID
    document_kind: str | None
    document_id: uuid.UUID | None
    mode: str
    reason: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    released_at: datetime | None
    released_by: uuid.UUID | None


# ---------------------------------------------------------------- plan
class PlanDocument(_Out):
    document_kind: str
    document_id: uuid.UUID
    number: str | None
    due_date: date | None
    days_overdue: int
    currency: str
    open_amount: Decimal
    disputed_amount: Decimal
    remindable_amount: Decimal


class PlanItem(_Out):
    step_id: uuid.UUID
    position: int
    offset_days: int
    tone: str
    audience: str
    email_template_id: uuid.UUID | None
    send_class: Literal["pre_due_auto_eligible", "needs_approval"]
    create_task: bool
    currency: str
    total_remindable: Decimal
    partially_disputed: bool
    documents: list[PlanDocument]


class Excluded(_Out):
    document_id: uuid.UUID
    number: str | None
    reason: str


class CollectionPlanOut(_Out):
    client_id: uuid.UUID
    today: date
    rule_id: uuid.UUID | None
    rule_name: str | None
    held: bool = False
    items: list[PlanItem] = Field(default_factory=list)
    excluded: list[Excluded] = Field(default_factory=list)


# ---------------------------------------------------------------- log
class LogOut(_Out):
    id: uuid.UUID
    client_id: uuid.UUID
    document_ids: list[uuid.UUID]
    event: str
    label: str
    rule_step_id: uuid.UUID | None
    tone: str | None
    email_message_id: uuid.UUID | None
    collection_activity_id: uuid.UUID | None
    agent_run_id: uuid.UUID | None
    proposal_id: uuid.UUID | None
    proposal_human_id: str | None = None
    actor: dict[str, Any]
    actor_name: str | None = None
    summary: str | None
    at: datetime


class SendOut(_Out):
    email_message_id: uuid.UUID
    collection_activity_id: uuid.UUID | None


class AgentStateOut(_Out):
    client_id: uuid.UUID
    state: Literal["active", "paused", "taken_over"]
    holds: list[HoldOut] = Field(default_factory=list)


__all__ = [
    "AUTO_RULE_MESSAGE",
    "AgentStateOut",
    "Audience",
    "CollectionPlanOut",
    "Excluded",
    "HoldIn",
    "HoldOut",
    "LogOut",
    "PlanDocument",
    "PlanItem",
    "RuleIn",
    "RuleOut",
    "RuleUpdate",
    "SendOut",
    "StepIn",
    "StepOut",
    "StepsIn",
    "Tone",
]
