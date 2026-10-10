# ruff: noqa: E501
"""Demand insights DTOs (Epic 55, stories 55.2-55.5): agent tool inputs (``extra='forbid'``), field sources,
suggestions, flags, similarity and SLA risk. Hand-mirrored in ``Frontend/src/features/agents/demand/types.ts``."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
SkillKind = Literal["mandatory", "preferred"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ drafts
class DraftDemandIn(_In):
    """``demands.create_draft``: exactly one mode -- intake (``message_id`` + ``role_index``) or direct (Copilot,
    ``client_id`` + ``title`` + ``positions``)."""

    message_id: uuid.UUID | None = None
    role_index: Annotated[int, Field(ge=0, le=4)] | None = None
    title_override: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None
    ) = None
    positions_override: Annotated[int, Field(ge=1, le=500)] | None = None
    client_id: uuid.UUID | None = None
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = (
        None
    )
    positions: Annotated[int, Field(ge=1, le=500)] | None = None
    location: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    work_mode: Literal["onsite", "hybrid", "remote"] | None = None
    employment_type: Literal["contract", "contract_to_hire", "permanent", "freelance"] | None = None
    min_experience_years: Annotated[Decimal, Field(ge=0, le=99)] | None = None
    max_experience_years: Annotated[Decimal, Field(ge=0, le=99)] | None = None
    start_date: date | None = None
    mandatory_skills: list[Label] = Field(default_factory=list, max_length=20)
    preferred_skills: list[Label] = Field(default_factory=list, max_length=20)
    notes: Annotated[str, StringConstraints(max_length=2000)] | None = None

    @model_validator(mode="after")
    def _one_mode(self) -> "DraftDemandIn":
        intake = self.message_id is not None
        direct = self.client_id is not None
        if intake == direct:
            msg = "give either message_id + role_index (intake) or client_id + title + positions (direct)"
            raise ValueError(msg)
        if intake:
            if self.role_index is None:
                msg = "role_index is required with message_id"
                raise ValueError(msg)
            direct_fields = (
                self.title, self.positions, self.location, self.work_mode, self.employment_type,
                self.min_experience_years, self.max_experience_years, self.start_date, self.notes,
            )  # fmt: skip
            if any(v is not None for v in direct_fields) or self.mandatory_skills or self.preferred_skills:
                msg = "intake mode takes values from the stored extraction; only title_override / positions_override"
                raise ValueError(msg)
        else:
            if self.title is None or self.positions is None:
                msg = "direct mode needs title and positions"
                raise ValueError(msg)
            if (
                self.role_index is not None
                or self.title_override is not None
                or self.positions_override is not None
            ):
                msg = "role_index / overrides are intake-mode fields"
                raise ValueError(msg)
        return self

    @property
    def mode(self) -> Literal["intake", "direct"]:
        return "intake" if self.message_id is not None else "direct"


class DraftDemandOut(_Out):
    demand_id: uuid.UUID
    human_id: str


class SkillItemIn(_In):
    label: Label
    skill_ref_kind: Literal["global", "tenant"] | None = None
    skill_id: uuid.UUID | None = None
    skill_kind: SkillKind = "mandatory"
    min_proficiency: Annotated[int, Field(ge=1, le=5)] | None = None


class DraftFieldsIn(_In):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = (
        None
    )
    location: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    work_mode: Literal["onsite", "hybrid", "remote"] | None = None
    employment_type: Literal["contract", "contract_to_hire", "permanent", "freelance"] | None = None
    positions: Annotated[int, Field(ge=1, le=500)] | None = None
    min_experience_years: Annotated[Decimal, Field(ge=0, le=99)] | None = None
    max_experience_years: Annotated[Decimal, Field(ge=0, le=99)] | None = None
    start_date: date | None = None


class UpdateDraftIn(_In):
    demand_id: uuid.UUID
    jd_content: Annotated[str, StringConstraints(min_length=1, max_length=20_000)] | None = None
    skills: list[SkillItemIn] | None = Field(default=None, max_length=30)
    fields: DraftFieldsIn | None = None


class DemandUpdateIn(_In):
    """``demands.update`` (high; always a Proposal): a non-draft demand's JD and / or skills only."""

    demand_id: uuid.UUID
    jd_content: Annotated[str, StringConstraints(min_length=1, max_length=20_000)] | None = None
    skills: list[SkillItemIn] | None = Field(default=None, max_length=30)

    @model_validator(mode="after")
    def _something(self) -> "DemandUpdateIn":
        if self.jd_content is None and self.skills is None:
            msg = "give jd_content and / or skills"
            raise ValueError(msg)
        return self


class UpdateDraftOut(_Out):
    demand_id: uuid.UUID
    jd_version_id: uuid.UUID | None = None
    changed_fields: list[str] = []


class SuggestSkillItem(_In):
    label: Label
    skill_ref_kind: Literal["global", "tenant"] | None = None
    skill_id: uuid.UUID | None = None
    skill_kind: SkillKind = "mandatory"
    min_proficiency: Annotated[int, Field(ge=1, le=5)] | None = None
    min_years: Annotated[Decimal, Field(ge=0, le=50)] | None = None
    evidence: Annotated[str, StringConstraints(max_length=300)] | None = None


class SuggestSkillsIn(_In):
    demand_id: uuid.UUID
    items: list[SuggestSkillItem] = Field(min_length=1, max_length=30)


class SuggestSkillsOut(_Out):
    suggestion_ids: list[uuid.UUID]


# ================================================================ insights (UI)
class FieldSourceOut(BaseModel):
    field: str
    source_quote: str


class SuggestionOut(BaseModel):
    id: uuid.UUID
    kind: Literal["skill", "new_skill", "contract", "rate_card"]
    payload: dict[str, Any]
    status: str
    created_at: datetime
    decided_at: datetime | None


class ReasonOut(BaseModel):
    field: str
    detail: str


class NextStep(BaseModel):
    label: str
    url: str


class RelatedDemandOut(BaseModel):
    related_visible: bool
    demand_id: uuid.UUID | None = None
    human_id: str | None = None
    title: str | None = None
    status: str | None = None


class FlagOut(BaseModel):
    id: uuid.UUID
    demand_id: uuid.UUID
    demand_human_id: str | None = None
    kind: Literal["possible_duplicate", "sla_risk"]
    status: str
    score: Decimal | None
    reasons: list[ReasonOut]
    summary: str | None
    milestone: str | None
    basis: str | None
    p_late: Decimal | None
    next_steps: list[NextStep]
    related: RelatedDemandOut | None
    raised_by_kind: str
    run_id: uuid.UUID | None
    created_at: datetime
    resolved_at: datetime | None
    resolution_note: str | None


class FlagQuery(BaseModel):
    kind: Literal["possible_duplicate", "sla_risk"] | None = None
    status: str | None = "open"
    demand_id: uuid.UUID | None = None


class ResolveFlagIn(_In):
    resolution: Literal["linked_duplicate", "distinct", "dismissed"]
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class AcceptSuggestionIn(_In):
    min_proficiency: Annotated[int, Field(ge=1, le=5)] | None = None
    skill_kind: SkillKind | None = None


class SlaClockOut(BaseModel):
    milestone: str
    status: str
    due_at: datetime
    elapsed_pct: int


class SlaRiskOut(BaseModel):
    demand_id: uuid.UUID
    at_risk: bool
    basis: Literal["prediction", "pipeline_only", "late", "none"]
    basis_text: str
    p_late: Decimal | None
    milestone: str | None
    clocks: list[SlaClockOut]
    counts: dict[str, int]
    reasons: list[str]
    next_steps: list[NextStep]


class LastRunOut(BaseModel):
    run_id: uuid.UUID
    status: str
    created_at: datetime


class DemandInsightsOut(BaseModel):
    demand_id: uuid.UUID
    field_sources: list[FieldSourceOut]
    suggestions: list[SuggestionOut]
    flags: list[FlagOut]
    sla: SlaRiskOut | None
    agent_enabled: bool
    last_run: LastRunOut | None
    pending_jd_proposal_id: uuid.UUID | None = None


class InsightsSummaryOut(BaseModel):
    open_flags: list[dict[str, Any]]
    pending_suggestions: int


class SimilarityOut(BaseModel):
    score: Decimal
    reasons: list[ReasonOut]
