"""Custom report builder DTOs (Epic 47). Every input model is ``extra="forbid"``: there is no free-text
expression field anywhere. The report ``definition`` is the foundation ``Definition`` (validated by
``core.semantic.validate`` so errors carry the ``semantic_*`` codes and ``details.path``); ``viz`` is the only
report-specific JSON and may only name keys of the definition's group_by / measures / fields.
"""

import uuid
from datetime import datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BuilderSettingsIn",
    "BuilderSettingsOut",
    "ExportIn",
    "ExportOut",
    "LimitHitOut",
    "PinOrderIn",
    "PinOut",
    "PreviewIn",
    "RepairIssueOut",
    "ReportCreate",
    "ReportOut",
    "ReportUpdate",
    "RunLogOut",
    "RunResultOut",
    "ScheduleCreate",
    "ScheduleOut",
    "SchedulePreviewOut",
    "ScheduleUpdate",
    "ShareIn",
    "ShareOut",
    "VizSpec",
]

_STRICT = ConfigDict(extra="forbid")
VizType = Literal["table", "pivot", "bar", "line", "pie", "kpi"]
ExportFormat = Literal["xlsx", "csv", "pdf"]
_KEY = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$"


class VizSpec(BaseModel):
    """``x`` / ``series``: group_by field keys; ``value``: a measure key (or a field key for a table)."""

    model_config = _STRICT

    type: VizType = "table"
    x: str | None = Field(default=None, pattern=_KEY, max_length=80)
    series: str | None = Field(default=None, pattern=_KEY, max_length=80)
    value: str | None = Field(default=None, pattern=_KEY, max_length=80)


class ReportCreate(BaseModel):
    model_config = _STRICT

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    definition: dict[str, Any]
    viz: VizSpec = Field(default_factory=VizSpec)


class ReportUpdate(BaseModel):
    model_config = _STRICT

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    definition: dict[str, Any] | None = None
    viz: VizSpec | None = None


class PreviewIn(BaseModel):
    model_config = _STRICT

    definition: dict[str, Any]
    viz: VizSpec = Field(default_factory=VizSpec)


class ExportIn(BaseModel):
    model_config = _STRICT

    format: ExportFormat = "xlsx"


class ShareIn(BaseModel):
    model_config = _STRICT

    target_kind: Literal["team", "role", "tenant"]
    team_id: uuid.UUID | None = None
    role_id: uuid.UUID | None = None


class ShareOut(BaseModel):
    target_kind: str
    team_id: uuid.UUID | None = None
    role_id: uuid.UUID | None = None
    name: str | None = None


class RepairIssueOut(BaseModel):
    path: str
    field: str | None = None
    reason: str


class ReportOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    owner_name: str | None = None
    source_key: str
    source_label: str | None = None
    definition: dict[str, Any]
    definition_version: int
    viz: VizSpec
    visibility: str
    status: str
    repair_issues: list[RepairIssueOut]
    shares: list[ShareOut]
    schedule_count: int = 0
    pinned: bool = False
    is_owner: bool = False
    last_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ColumnOut(BaseModel):
    key: str
    label: str
    type: str
    masked: bool = False


class LimitHitOut(BaseModel):
    kind: Literal["row_cap", "timeout"]
    message: str


class RunResultOut(BaseModel):
    run_id: uuid.UUID | None
    report_id: uuid.UUID | None = None
    definition_version: int | None = None
    columns: list[ColumnOut]
    rows: list[dict[str, Any]]
    total: int | None
    truncated: bool
    row_limit: int
    masked_fields: list[str]
    limit_hit: LimitHitOut | None = None
    viz: VizSpec


class ExportOut(BaseModel):
    run_id: uuid.UUID
    mode: Literal["sync", "async"]
    status: str
    format: ExportFormat
    row_count: int | None = None
    masked_fields: list[str] = []
    limit_hit: LimitHitOut | None = None
    download_url: str | None = None


class PinOut(BaseModel):
    report_id: uuid.UUID
    name: str
    position: int
    viz: VizSpec
    status: str
    definition_version: int


class PinOrderIn(BaseModel):
    model_config = _STRICT

    report_ids: list[uuid.UUID] = Field(max_length=100)


class ScheduleCreate(BaseModel):
    model_config = _STRICT

    report_id: uuid.UUID
    cadence: Literal["daily", "weekly", "monthly"]
    weekday: int | None = Field(default=None, ge=0, le=6)
    month_day: int | None = Field(default=None, ge=-1, le=28)
    local_time: time
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    format: ExportFormat = "xlsx"
    recipient_user_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)


class ScheduleUpdate(BaseModel):
    model_config = _STRICT

    cadence: Literal["daily", "weekly", "monthly"] | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    month_day: int | None = Field(default=None, ge=-1, le=28)
    local_time: time | None = None
    timezone: str | None = Field(default=None, max_length=64)
    format: ExportFormat | None = None
    recipient_user_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=50)
    status: Literal["active", "paused"] | None = None


class ScheduleOut(BaseModel):
    id: uuid.UUID
    report_id: uuid.UUID
    report_name: str | None = None
    owner_id: uuid.UUID
    cadence: str
    weekday: int | None
    month_day: int | None
    local_time: time
    timezone: str
    format: str
    recipient_user_ids: list[uuid.UUID]
    status: str
    paused_reason: str | None
    consecutive_failures: int
    next_run_at: datetime | None
    last_run_at: datetime | None
    created_at: datetime


class SchedulePreviewOut(BaseModel):
    next_runs: list[datetime]


class RunLogOut(BaseModel):
    id: uuid.UUID
    report_id: uuid.UUID | None
    report_name: str | None = None
    source_key: str
    definition_version: int | None
    schedule_id: uuid.UUID | None
    slot_at: datetime | None
    run_kind: str
    principal_user_id: uuid.UUID
    principal_name: str | None = None
    status: str
    limit_kind: str | None
    skip_reason: str | None
    error_code: str | None
    row_count: int | None
    duration_ms: int | None
    masked_fields: list[str]
    format: str | None
    delivery: str
    expires_at: datetime | None
    started_at: datetime
    finished_at: datetime | None


class BuilderSettingsOut(BaseModel):
    preview_rows: int
    export_row_cap: int
    statement_timeout_s: int
    attachment_max_mb: int
    link_ttl_days: int
    max_recipients: int
    max_schedules: int


class BuilderSettingsIn(BaseModel):
    """Caps may be lowered below the platform maximums, never raised."""

    model_config = _STRICT

    preview_rows: int | None = Field(default=None, ge=1, le=500)
    export_row_cap: int | None = Field(default=None, ge=1, le=100_000)
    statement_timeout_s: int | None = Field(default=None, ge=1, le=30)
    attachment_max_mb: int | None = Field(default=None, ge=1, le=25)
    link_ttl_days: int | None = Field(default=None, ge=1, le=30)
    max_recipients: int | None = Field(default=None, ge=1, le=50)
    max_schedules: int | None = Field(default=None, ge=1, le=500)
