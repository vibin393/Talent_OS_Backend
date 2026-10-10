# ruff: noqa: E501
"""Sourcing & Matching Agent DTOs (Epic 56). Hand-mirrored in ``Frontend/src/features/agents/sourcing/types.ts``."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Channel = Literal["email", "sms", "whatsapp"]
SourceKind = Literal["match", "pool", "silver_medalist", "search", "rediscovery"]
EntryStatus = Literal["suggested", "added_to_pipeline", "dismissed"]
DraftStatus = Literal["none", "proposed", "sent", "failed", "suppressed"]
DismissReason = Literal[
    "not_interested",
    "skills_gap",
    "experience",
    "compensation",
    "location",
    "availability",
    "duplicate",
    "other",
]
TriggerKey = Literal["demand_opened", "requirements_changed", "weekly_refresh", "manual", "workflow"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(BaseModel):
    kind: SourceKind
    ref: str | None = None
    label: str | None = None


class ChannelState(BaseModel):
    ok: bool
    reason: str | None = None


class Contactability(BaseModel):
    email: ChannelState
    sms: ChannelState
    whatsapp: ChannelState
    best_channel: Channel | None = None


class EntryOut(BaseModel):
    id: uuid.UUID
    shortlist_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None = None
    candidate_human_id: str | None = None
    current_title: str | None = None
    rank: int
    overall: float | None = None
    match_id: uuid.UUID | None = None
    sources: list[SourceRef]
    explanation: str | None = None
    agent_note: str | None = None
    flags: list[str]
    contactability: dict[str, ChannelState]
    best_channel: Channel | None = None
    last_activity_at: datetime | None = None
    notice_period_days: int | None = None
    availability_date: date | None = None
    status: EntryStatus
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    dismiss_reason: str | None = None
    application_id: uuid.UUID | None = None
    draft_channel: Channel | None = None
    draft_subject: str | None = None
    draft_body: str | None = None
    draft_status: DraftStatus
    outreach_proposal_id: uuid.UUID | None = None
    outreach_sent_at: datetime | None = None


class ShortlistVersionOut(BaseModel):
    id: uuid.UUID
    version: int
    status: Literal["current", "superseded"]
    trigger_key: TriggerKey
    created_at: datetime
    entry_count: int


class ShortlistOut(BaseModel):
    id: uuid.UUID
    demand_id: uuid.UUID
    run_id: uuid.UUID | None
    version: int
    status: Literal["current", "superseded"]
    trigger_key: TriggerKey
    considered_count: int
    excluded_counts: dict[str, int]
    suppressed_counts: dict[str, int]
    top_n: int
    summary: str | None
    created_at: datetime
    owner_user_id: uuid.UUID | None = None
    owner_name: str | None = None
    agent_enabled: bool = False
    entries: list[EntryOut]


class ShortlistEnvelope(BaseModel):
    """``GET /demands/{id}/agent-shortlist``: ``shortlist`` is null before the first run."""

    demand_id: uuid.UUID
    agent_enabled: bool
    shortlist: ShortlistOut | None


class EntryDecisionIn(_In):
    decision: Literal["add_to_pipeline", "dismiss", "restore"]
    reason: DismissReason | None = None


class SourcingSettingsOut(BaseModel):
    top_n: int
    draft_top: int
    preferred_channel: Channel
    include_silver_medalists: bool
    max_searches: int
    propose_pipeline_adds: bool
    propose_sequence_id: uuid.UUID | None
    updated_at: datetime | None = None


class SourcingSettingsIn(_In):
    top_n: int | None = Field(default=None, ge=5, le=100)
    draft_top: int | None = Field(default=None, ge=0, le=25)
    preferred_channel: Channel | None = None
    include_silver_medalists: bool | None = None
    max_searches: int | None = Field(default=None, ge=0, le=5)
    propose_pipeline_adds: bool | None = None
    propose_sequence_id: uuid.UUID | None = None
    clear_sequence: bool = False


class SourcingRunOut(BaseModel):
    run_id: uuid.UUID | None
    status: str
    reason: str | None = None


class CandidateFacts(BaseModel):
    """Profile facts used by ranking / the shortlist (never written back)."""

    candidate_id: uuid.UUID
    last_activity_at: datetime | None = None
    notice_period_days: int | None = None
    availability_date: date | None = None
    ai_consent_state: str = "pending"


class CandidateSet(BaseModel):
    """``build_candidate_set`` result: ranked-ready rows plus the exclusion counts."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    demand_id: uuid.UUID
    rows: list[dict[str, Any]]
    considered_count: int
    excluded_counts: dict[str, int]
    dropped_not_found: list[uuid.UUID] = Field(default_factory=list)


__all__ = [
    "CandidateFacts",
    "CandidateSet",
    "Channel",
    "ChannelState",
    "Contactability",
    "DismissReason",
    "EntryDecisionIn",
    "EntryOut",
    "ShortlistEnvelope",
    "ShortlistOut",
    "ShortlistVersionOut",
    "SourceRef",
    "SourcingRunOut",
    "SourcingSettingsIn",
    "SourcingSettingsOut",
]
