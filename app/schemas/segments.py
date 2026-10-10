"""Segment DTOs (Epic 38; Epic 36 contract, AD-39). Frozen; changing a field needs a spine amendment."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = ["SegmentEvaluation"]


class SegmentEvaluation(BaseModel):
    """Result of ``segments_service.evaluate``; ``subject_ids`` is empty in ``count`` mode."""

    model_config = ConfigDict(frozen=True)

    segment_id: uuid.UUID | None
    source: str
    definition_version: int
    count: int
    subject_ids: tuple[uuid.UUID, ...] = ()
    reasons: dict[uuid.UUID, dict[str, Any]] | None = None
    evaluated_at: datetime


# ---------------------------------------------------------------- Epic 38 API DTOs (not part of the contract)
from typing import Annotated, Literal  # noqa: E402

from pydantic import Field, StringConstraints  # noqa: E402

from app.schemas.semantic import ColumnMeta  # noqa: E402

Visibility = Literal["private", "team", "tenant"]
SegmentName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SegmentCreate(_In):
    name: SegmentName
    description: Description | None = None
    source_key: str = Field(max_length=60)
    definition: dict[str, Any]
    visibility: Visibility = "private"
    team_id: uuid.UUID | None = None


class SegmentUpdate(_In):
    name: SegmentName | None = None
    description: Description | None = None
    definition: dict[str, Any] | None = None
    visibility: Visibility | None = None
    team_id: uuid.UUID | None = None


class SegmentOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    source_key: str
    definition: dict[str, Any]
    definition_version: int
    owner_id: uuid.UUID
    visibility: str
    team_id: uuid.UUID | None
    needs_repair: bool
    repair_issues: list[dict[str, Any]]
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SegmentVersionOut(BaseModel):
    version: int
    definition: dict[str, Any]
    created_by: uuid.UUID | None
    created_at: datetime


class SegmentPreviewIn(_In):
    source_key: str = Field(max_length=60)
    definition: dict[str, Any]


class SegmentPreviewOut(BaseModel):
    count: int
    columns: list[ColumnMeta]
    rows: list[dict[str, Any]]
    masked_fields: list[str]


__all__ += [
    "SegmentCreate",
    "SegmentOut",
    "SegmentPreviewIn",
    "SegmentPreviewOut",
    "SegmentUpdate",
    "SegmentVersionOut",
]
