# ruff: file-ignore[line-too-long]
"""Analytics Agent DTOs (Epic 64). Every input model is ``extra="forbid"``: the only structured input the model controls is
a foundation ``Definition`` (closed operators, registered keys) plus a report ``VizSpec`` and short labels -- there is no
SQL, expression or formula field anywhere (NFR-46)."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.semantic.schema import Definition
from app.schemas.report_builder import VizSpec

__all__ = [
    "AnswerColumnOut",
    "AnswerOut",
    "AnswerSavedIn",
    "AnswerSummaryOut",
    "CatalogueFieldOut",
    "CatalogueIn",
    "CatalogueOut",
    "CatalogueSourceOut",
    "IssueOut",
    "LimitHitOut",
    "ReportGetIn",
    "ReportGetOut",
    "ReportListRowOut",
    "ReportsListIn",
    "ReportsListOut",
    "RunQuestionIn",
    "RunQuestionOut",
    "ValidateIn",
    "ValidateOut",
]

_STRICT = ConfigDict(extra="forbid")
Assumption = Annotated[str, Field(min_length=1, max_length=200)]


# ================================================================ tool inputs
class CatalogueIn(BaseModel):
    model_config = _STRICT

    source_key: str | None = Field(default=None, max_length=80)


class ValidateIn(BaseModel):
    model_config = _STRICT

    definition: Definition
    viz: VizSpec = Field(default_factory=VizSpec)


class RunQuestionIn(BaseModel):
    model_config = _STRICT

    definition: Definition
    viz: VizSpec = Field(default_factory=VizSpec)
    question: str = Field(min_length=1, max_length=500)
    assumptions: list[Assumption] = Field(default_factory=list, max_length=5)


class ReportsListIn(BaseModel):
    model_config = _STRICT

    q: str | None = Field(default=None, max_length=120)
    source_key: str | None = Field(default=None, max_length=80)
    limit: int = Field(default=10, ge=1, le=20)


class ReportGetIn(BaseModel):
    model_config = _STRICT

    report_id: uuid.UUID


# ================================================================ tool outputs
class CatalogueFieldOut(BaseModel):
    key: str
    label: str
    type: str
    ops: list[str]
    aggs: list[str]
    groupable: bool
    date_buckets: bool
    enum_values: list[str] | None = None
    masked: bool = False


class CatalogueSourceOut(BaseModel):
    key: str
    label: str
    field_count: int


class CatalogueOut(BaseModel):
    """No ``source_key``: ``sources``. With one: ``key`` / ``label`` / ``fields`` / ``joins``."""

    sources: list[CatalogueSourceOut] | None = None
    key: str | None = None
    label: str | None = None
    fields: list[CatalogueFieldOut] | None = None
    joins: list[str] | None = None


class IssueOut(BaseModel):
    code: str
    path: str
    message: str


class ValidateOut(BaseModel):
    valid: bool
    errors: list[IssueOut] = []


class AnswerColumnOut(BaseModel):
    key: str
    label: str
    type: str
    masked: bool = False


class LimitHitOut(BaseModel):
    kind: Literal["row_cap", "timeout"]
    message: str


class RunQuestionOut(BaseModel):
    """Model-facing result. ``rows`` are arrays aligned with ``columns`` (so every cell sits at the untrusted path
    ``rows`` and is wrapped as data); suppressed groups are left out and counted in ``suppressed_cells``."""

    answer_id: uuid.UUID
    columns: list[AnswerColumnOut]
    rows: list[list[Any]]
    total: int | None
    truncated: bool
    masked_fields: list[str]
    limit_hit: LimitHitOut | None = None
    suppressed_cells: int = 0
    explanation: str


class ReportListRowOut(BaseModel):
    id: uuid.UUID
    name: str
    source_key: str
    description: str | None = None
    needs_repair: bool = False


class ReportsListOut(BaseModel):
    rows: list[ReportListRowOut]


class ReportGetOut(BaseModel):
    id: uuid.UUID
    name: str
    definition: dict[str, Any]
    viz: VizSpec


# ================================================================ API
class AnswerOut(BaseModel):
    id: uuid.UUID
    question: str
    assumptions: list[str]
    source_key: str
    source_label: str | None = None
    definition: dict[str, Any]
    definition_version: int
    viz: VizSpec
    explanation: str
    masked_fields: list[str]
    row_count: int | None
    truncated: bool
    limit_kind: str | None
    suppressed_cells: int
    min_cell_size: int | None = None
    status: str
    saved_report_id: uuid.UUID | None
    saved_report_name: str | None = None
    conversation_id: uuid.UUID | None
    run_id: uuid.UUID | None
    created_at: datetime
    expires_at: datetime


class AnswerSummaryOut(BaseModel):
    id: uuid.UUID
    question: str
    source_key: str
    source_label: str | None = None
    status: str
    row_count: int | None
    saved_report_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    run_id: uuid.UUID | None = None
    created_at: datetime


class AnswerSavedIn(BaseModel):
    model_config = _STRICT

    report_id: uuid.UUID
