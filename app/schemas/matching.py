"""Matching API schemas (Epic 5, stories 5.4/5.5) and the explanation output schema of the AI gateway."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

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
ExplanationStatus = Literal["pending", "ready", "failed", "skipped"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ settings
class MatchingSettingsIn(_In):
    weights: dict[str, Any] | None = None
    strong_threshold: Annotated[int, Field(ge=0, le=100)] | None = None
    relevant_threshold: Annotated[int, Field(ge=0, le=100)] | None = None
    proxy_fields: Annotated[list[str], Field(max_length=20)] | None = None
    explain_top_n: Annotated[int, Field(ge=0, le=100)] | None = None
    fx_rates: dict[str, Decimal] | None = None


class MatchingSettingsOut(BaseModel):
    weights: dict[str, int]
    strong_threshold: int
    relevant_threshold: int
    proxy_fields: list[str]
    explain_top_n: int
    fx_rates: dict[str, str]
    available_proxy_fields: list[str]


# ================================================================ matches
class MatchCandidateOut(BaseModel):
    id: uuid.UUID
    human_id: str | None = None
    display_name: str | None = None
    current_title: str | None = None
    current_employer: str | None = None
    city: str | None = None


class EvidenceOut(BaseModel):
    id: str
    kind: str
    text: str | None
    comp: bool = False


class MatchOut(BaseModel):
    id: uuid.UUID
    demand_id: uuid.UUID
    rank: int | None = None
    candidate: MatchCandidateOut
    # Epic 49: ``None`` when an AEDT score suppressor hides the pair (``score_hidden_reason`` says why).
    overall: float | None
    sub_scores: dict[str, int | None]
    missing_mandatory: list[str]
    evidence: list[EvidenceOut]
    explanation: str | None
    explanation_status: ExplanationStatus
    computed_at: datetime
    dismissed_at: datetime | None = None
    dismissed_by: uuid.UUID | None = None
    dismiss_reason: DismissReason | None = None
    dismiss_note: str | None = None
    added_to_pipeline_at: datetime | None = None
    application_id: uuid.UUID | None = None
    is_stale: bool = False
    score_hidden_reason: str | None = None  # Epic 49: "alternative_process" | "notice_period"


class FunnelOut(BaseModel):
    total: int = 0
    relevant: int = 0
    strong: int = 0
    available: int = 0
    strong_threshold: int | None = None


class RunOut(BaseModel):
    id: uuid.UUID
    status: Literal["running", "completed", "failed", "skipped"]
    trigger: str
    started_at: datetime
    finished_at: datetime | None = None
    considered: int = 0


class MatchSectionOut(BaseModel):
    """Epic 41 (additive): a registered section rendered above the match list (stored scores only)."""

    key: str
    label: str
    order: int
    total: int
    items: list[MatchOut]


class MatchListOut(BaseModel):
    items: list[MatchOut]
    total: int
    page: int
    page_size: int
    can_add_to_pipeline: bool
    last_run: RunOut | None = None
    funnel: FunnelOut | None = None
    sections: list[MatchSectionOut] | None = None  # Epic 41 (additive): registered sections, page 1 only


class FunnelResponse(BaseModel):
    demand_id: uuid.UUID
    funnel: FunnelOut | None
    last_run: RunOut | None


class DismissIn(_In):
    reason: DismissReason
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None


class RecomputeOut(BaseModel):
    demand_id: uuid.UUID
    status: Literal["queued", "completed", "skipped", "failed", "running"]
    run_id: uuid.UUID | None = None


# ================================================================ AI output (gateway schema)
class MatchExplanation(BaseModel):
    """Output of ``match_explain.v1``: text only; numbers are never produced by the model."""

    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=280)]
    cited_evidence_ids: Annotated[
        list[Annotated[str, StringConstraints(max_length=100)]], Field(max_length=20)
    ] = Field(default_factory=list)


# Epic 38 (FR-194, decision 11): rediscovery summary = the P1 funnel plus registered sections (``pools``, ...).
class RediscoveryOut(BaseModel):
    demand_id: uuid.UUID
    funnel: FunnelResponse
    sections: dict[str, Any] = Field(default_factory=dict)
