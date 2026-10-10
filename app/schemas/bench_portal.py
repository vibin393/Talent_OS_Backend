"""Worker "My Work" bench schemas (Epic 24 A5). Never any cost field (field-visibility guard)."""

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.core.field_visibility import register_portal_schema
from app.schemas.bench import BenchOrigin, BenchStatus


@register_portal_schema("worker")
class WorkerBenchOut(BaseModel):
    id: uuid.UUID
    status: BenchStatus
    origin: BenchOrigin
    available_from: date
    work_authorization: str | None
    preferred_locations: list[str]


@register_portal_schema("worker")
class WorkerBenchState(BaseModel):
    entry: WorkerBenchOut | None
    can_opt_in: bool
    can_opt_out: bool


class OptInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_from: date
    work_authorization: str | None = Field(default=None, max_length=500)
    preferred_locations: list[str] = Field(default_factory=list, max_length=20)
