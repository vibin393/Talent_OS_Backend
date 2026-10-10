"""Skills DTOs (Epic 37; Epic 36 contract). Re-exports the skill references of ``app.core.skill_refs``.

Epic 37 adds the graph admin, evidence, suggestion and insight DTOs below. Weights and decimals serialise as
strings (no ``float`` in persisted weights, AD-41).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, StringConstraints

from app.core.skill_refs import EdgeKind, ExpandedSkill, SkillKey

__all__ = [
    "EdgeKind",
    "EdgeOrigin",
    "EvidenceIn",
    "EvidenceKind",
    "EvidenceOut",
    "ExpandPreviewIn",
    "ExpandPreviewOut",
    "ExpandedSkill",
    "ExpandedSkillOut",
    "PathHopOut",
    "SkillEdgeIn",
    "SkillEdgeOut",
    "SkillEdgeUpdate",
    "SkillGraphSettingsIn",
    "SkillGraphSettingsOut",
    "SkillInferenceItem",
    "SkillInferenceOut",
    "SkillInsightRow",
    "SkillInsightsOut",
    "SkillKey",
    "SkillNodeOut",
    "SkillProfileRow",
    "SkillSuggestionAcceptIn",
    "SkillSuggestionOut",
    "SkillSuggestionRejectIn",
]

DecimalStr = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str, when_used="json")]
SkillKeyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=60)]
EdgeOrigin = Literal["platform", "tenant", "legacy"]
# Epic 60: + interview, interview_declared
EvidenceKind = Literal[
    "resume", "self", "recruiter", "assessment", "project", "graph", "interview", "interview_declared"
]
Proficiency = Annotated[int, Field(ge=1, le=5)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ graph admin
class SkillLabel(BaseModel):
    key: str
    name: str
    category: str | None = None
    status: str = "approved"


class SkillEdgeOut(BaseModel):
    id: uuid.UUID | None  # None for legacy tenant parent links
    origin: EdgeOrigin
    from_skill: SkillLabel
    to_skill: SkillLabel
    kind: EdgeKind
    weight: DecimalStr
    note: str | None = None
    source: str | None = None
    updated_at: datetime | None = None


class SkillEdgeIn(_In):
    from_key: SkillKeyStr
    to_key: SkillKeyStr
    kind: EdgeKind
    weight: Any  # validated by the service (422 skill_edge_weight_invalid)
    note: Note | None = None


class SkillEdgeUpdate(_In):
    weight: Any = None
    note: Note | None = None


class SkillNodeOut(BaseModel):
    skill: SkillLabel
    aliases: list[str]
    parent: SkillLabel | None = None
    outgoing: list[SkillEdgeOut]
    incoming: list[SkillEdgeOut]


class SkillGraphSettingsOut(BaseModel):
    expansion_default: bool
    match_depth: int
    min_weight: DecimalStr
    inference_auto_suggest: bool
    graph_version: str


class SkillGraphSettingsIn(_In):
    expansion_default: bool | None = None
    match_depth: Annotated[int, Field(ge=1, le=2)] | None = None
    min_weight: Any = None
    inference_auto_suggest: bool | None = None


class ExpandPreviewIn(_In):
    refs: Annotated[list[SkillKeyStr], Field(min_length=1, max_length=20)]
    depth: int = 1
    min_weight: Any = None


class PathHopOut(BaseModel):
    from_key: str
    to_key: str
    kind: EdgeKind
    weight: DecimalStr
    label: str  # e.g. "Spring Boot implies Java"


class ExpandedSkillOut(BaseModel):
    key: str
    name: str
    root: str
    weight: DecimalStr
    depth: int
    via: str | None
    path: list[PathHopOut]


class ExpandPreviewOut(BaseModel):
    items: list[ExpandedSkillOut]
    graph_version: str


# ================================================================ evidence
class EvidenceIn(_In):
    kind: EvidenceKind
    proficiency: Proficiency | None = None
    years: Annotated[Decimal, Field(ge=0, le=60)] | None = None
    last_used: date | None = None
    note: Note | None = None
    source_ref: Annotated[str, StringConstraints(max_length=200)] | None = None


class EvidenceOut(BaseModel):
    id: uuid.UUID
    candidate_skill_id: uuid.UUID
    kind: EvidenceKind
    proficiency: int | None
    years: Decimal | None
    last_used: date | None
    source_ref: str | None
    note: str | None
    observed_at: datetime
    created_by_kind: str
    created_by_id: uuid.UUID | None
    created_by_name: str | None = None
    superseded_at: datetime | None
    proficiency_after: int | None = None
    verification_after: str | None = None


class SkillProfileRow(BaseModel):
    candidate_skill_id: uuid.UUID
    skill_key: str
    name: str
    proficiency: int | None
    verification: Literal["declared", "inferred", "verified"]
    evidence_count: int


# ================================================================ suggestions
class SkillSuggestionOut(BaseModel):
    id: uuid.UUID
    candidate_id: uuid.UUID
    skill_key: str
    name: str
    proposed_proficiency: int | None
    source: Literal["ai_resume", "graph", "project", "assessment", "interview"]  # Epic 60: + interview
    rationale: str | None
    status: Literal["pending", "accepted", "rejected", "edited"]
    created_at: datetime
    decided_at: datetime | None = None


class SkillSuggestionAcceptIn(_In):
    proficiency: Proficiency | None = None


class SkillSuggestionRejectIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class SkillInferenceItem(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    proficiency: Annotated[int, Field(ge=1, le=5)] | None = None
    evidence_quote: Annotated[str, StringConstraints(max_length=200)] | None = None


class SkillInferenceOut(BaseModel):
    skills: Annotated[list[SkillInferenceItem], Field(max_length=40)] = Field(default_factory=list)


# ================================================================ insights
class SkillInsightRow(BaseModel):
    skill_key: str
    name: str
    category: str | None
    open_demand: int
    supply_by_proficiency: dict[str, int]  # "1".."5", "unknown"
    supply_total: int
    verified_supply: int
    bench: int | None  # None = caller lacks bench:read
    trend_90d: dict[str, list[int]]  # {"demand": [13 weeks], "supply": [13 weeks]}
    links: dict[str, dict[str, Any]]  # deep-link filter params: demands, candidates, bench


class SkillInsightsOut(BaseModel):
    items: list[SkillInsightRow]
    generated_at: datetime
    week_starts: list[date]
