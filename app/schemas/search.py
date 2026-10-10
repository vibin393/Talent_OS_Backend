"""Search API schemas (Epic 5, stories 5.1/5.3/5.6) and the NL-interpretation output schema (AI gateway)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SearchMode = Literal["boolean", "nl", "structured"]
SearchSort = Literal["relevance", "updated", "experience"]
Str200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateSearchFilters(_In):
    skills: Annotated[list[uuid.UUID], Field(max_length=50)] = Field(default_factory=list)
    skills_mode: Literal["any", "all"] = "any"
    experience_min: Annotated[Decimal, Field(ge=0, le=60)] | None = None
    experience_max: Annotated[Decimal, Field(ge=0, le=60)] | None = None
    countries: Annotated[list[Str200], Field(max_length=50)] = Field(default_factory=list)
    cities: Annotated[list[Str200], Field(max_length=50)] = Field(default_factory=list)
    work_auth: Annotated[list[Str200], Field(max_length=20)] = Field(default_factory=list)
    comp_min: Annotated[Decimal, Field(ge=0)] | None = None
    comp_max: Annotated[Decimal, Field(ge=0)] | None = None
    comp_currency: Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{3}$")] = "INR"
    notice_max_days: Annotated[int, Field(ge=0, le=365)] | None = None
    available_by: date | None = None
    certifications: Annotated[list[Str200], Field(max_length=20)] = Field(default_factory=list)
    current_employers: Annotated[list[Str200], Field(max_length=20)] = Field(default_factory=list)
    tags: Annotated[list[uuid.UUID], Field(max_length=20)] = Field(default_factory=list)
    sources: Annotated[list[Str200], Field(max_length=10)] = Field(default_factory=list)
    owner_ids: Annotated[list[uuid.UUID], Field(max_length=50)] = Field(default_factory=list)
    updated_after: datetime | None = None
    updated_before: datetime | None = None
    not_submitted_to_client_id: uuid.UUID | None = None
    # Epic 37 (FR-190): include related skills (skills graph expansion) in the skills filter.
    expand_skills: bool = True


class CandidateSearchIn(_In):
    q: Annotated[str, StringConstraints(max_length=1000)] | None = None
    mode: SearchMode = "boolean"
    filters: CandidateSearchFilters = Field(default_factory=CandidateSearchFilters)
    sort: SearchSort = "relevance"
    page: Annotated[int, Field(ge=1, le=1000)] = 1
    page_size: Annotated[int, Field(ge=1, le=100)] = 20
    semantic_text: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None


class SnippetSegment(BaseModel):
    text: str
    hl: bool


class SearchSkillOut(BaseModel):
    id: uuid.UUID
    name: str
    matched: bool = False


class CandidateHit(BaseModel):
    id: uuid.UUID
    human_id: str
    display_name: str
    current_title: str | None = None
    current_employer: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    experience_years: float | None = None
    notice_days: int | None = None
    available_from: date | None = None
    skills: list[SearchSkillOut] = Field(default_factory=list)
    snippet: list[SnippetSegment] = Field(default_factory=list)
    score: float | None = None
    owner_id: uuid.UUID
    updated_at: datetime


class FacetBucket(BaseModel):
    value: str
    label: str
    count: int


class Facet(BaseModel):
    key: str
    buckets: list[FacetBucket]
    approximate: bool = False


class InterpretationChip(BaseModel):
    key: str
    label: str
    value: Any


class CandidateSearchOut(BaseModel):
    items: list[CandidateHit]
    total: int
    page: int
    page_size: int
    facets: list[Facet]
    facets_approximate: bool = False
    interpretation: list[InterpretationChip] | None = None
    notice: Literal["ai_unavailable"] | None = None
    ai_action_id: uuid.UUID | None = None
    # NL mode: the structured state the chips stand for (the frontend re-queries with mode="structured").
    applied_q: str | None = None
    applied_filters: CandidateSearchFilters | None = None
    semantic_text: str | None = None


# ================================================================ global search
QuickHitType = Literal[
    "candidate", "demand", "client", "contact",
    # Epic 34 (A5): Phase 2 groups registered through ``global_search.register_global_group``.
    "vendor", "placement", "worker", "invoice",
    # Epic 36 (spec 0.7): Phase 3 groups (registered centrally by Epic 52 from owners' ``quick_search``).
    "talent_pool", "campaign", "assessment", "campus_drive", "recruitment_event", "project",
    "immigration_case", "report_definition",
    # Epic 65 (FR-329): Phase 4 groups (agent Proposals, Knowledge Sources).
    "agent_proposal", "knowledge_source",
]  # fmt: skip


class QuickHit(BaseModel):
    id: uuid.UUID
    type: QuickHitType
    title: str
    subtitle: str | None = None
    human_id: str | None = None
    url: str
    exact: bool = False  # Epic 34: an exact Invoice number / Placement id match (pinned first)


class GlobalGroup(BaseModel):
    type: QuickHitType
    items: list[QuickHit]
    has_more: bool = False
    timed_out: bool = False


class GlobalSearchOut(BaseModel):
    exact: QuickHit | None = None
    groups: list[GlobalGroup]


# ================================================================ AI output (gateway schema)
class CompMax(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Annotated[float, Field(ge=0)]
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{3}$")]


class NLInterpretation(BaseModel):
    """Output of ``nl_search_filters.v1``: structured filters only (validated; unknown keys rejected)."""

    model_config = ConfigDict(extra="forbid")

    keywords: Annotated[str, StringConstraints(max_length=500)] | None = None
    skills_all: Annotated[list[Annotated[str, StringConstraints(max_length=100)]], Field(max_length=15)] = (
        Field(default_factory=list)
    )
    skills_any: Annotated[list[Annotated[str, StringConstraints(max_length=100)]], Field(max_length=15)] = (
        Field(default_factory=list)
    )
    experience_min: Annotated[float, Field(ge=0, le=60)] | None = None
    experience_max: Annotated[float, Field(ge=0, le=60)] | None = None
    countries: Annotated[list[Annotated[str, StringConstraints(max_length=100)]], Field(max_length=10)] = (
        Field(default_factory=list)
    )
    cities: Annotated[list[Annotated[str, StringConstraints(max_length=100)]], Field(max_length=10)] = Field(
        default_factory=list
    )
    work_auth: Annotated[list[Annotated[str, StringConstraints(max_length=100)]], Field(max_length=10)] = (
        Field(default_factory=list)
    )
    notice_max_days: Annotated[int, Field(ge=0, le=365)] | None = None
    available_by: date | None = None
    comp_max: CompMax | None = None
    domains: Annotated[list[Annotated[str, StringConstraints(max_length=100)]], Field(max_length=10)] = Field(
        default_factory=list
    )
    semantic_text: Annotated[str, StringConstraints(max_length=300)] = ""
