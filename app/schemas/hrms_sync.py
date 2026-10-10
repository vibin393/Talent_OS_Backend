# ruff: noqa: E501
"""HRMS sync API DTOs (Epic 71, spec A11). Records carry ids, statuses and sanitised messages only -- never field
values; mapping rows carry catalogue keys and purposes, never personal data."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Transform = Literal["none", "date_iso", "date_dmy", "upper", "lower", "lookup"]
RecordStatus = Literal["pending", "blocked", "synced", "failed", "skipped"]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HrmsSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    push_worker_types: list[Literal["employee", "contractor"]] | None = Field(default=None, max_length=2)
    push_on_join: bool | None = None
    push_changes: bool | None = None
    push_exits: bool | None = None
    pull_hour_ist: int | None = Field(default=None, ge=0, le=23)


class HrmsSettingsOut(_Out):
    push_worker_types: list[str]
    push_on_join: bool
    push_changes: bool
    push_exits: bool
    pull_hour_ist: int


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hrms_field: str = Field(min_length=1, max_length=100)
    local_field: str | None = Field(default=None, max_length=60)
    constant: str | None = Field(default=None, max_length=200)
    transform: Transform = "none"
    lookup: dict[str, str] = Field(default_factory=dict, max_length=500)


class MappingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MappingIn] = Field(max_length=200)


class MappingOut(_Out):
    id: uuid.UUID
    hrms_field: str
    local_field: str | None
    constant: str | None
    transform: str
    lookup: dict[str, Any]
    sensitive: bool
    purpose: str | None
    enabled_by: uuid.UUID | None
    enabled_at: datetime | None


class HrmsFieldOut(BaseModel):
    hrms_field: str
    label: str
    required: bool
    kind: str
    options: list[dict[str, str]]
    custom: bool
    mapping: MappingOut | None


class CatalogueFieldOut(BaseModel):
    key: str
    label: str
    sensitive: bool


class MappingPageOut(BaseModel):
    installation_id: uuid.UUID
    legal_entity_id: uuid.UUID
    connector_key: str
    custom_fields_allowed: bool
    fields: list[HrmsFieldOut]
    catalogue: list[CatalogueFieldOut]


class SensitiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(min_length=5, max_length=500)


class SkipIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=2, max_length=40)


class IncludeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include: bool


class HrmsRecordOut(_Out):
    id: uuid.UUID
    installation_id: uuid.UUID
    legal_entity_id: uuid.UUID
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    include_override: bool | None
    employee_ref: str | None
    employee_code: str | None
    pushed_version: int
    last_push_kind: str | None
    status: str
    blocked_fields: list[str]
    skip_reason: str | None
    last_message: str | None
    last_pushed_at: datetime | None
    hrms_status: str | None
    hrms_exit_date: date | None
    last_pulled_at: datetime | None
    mismatch: str
    mismatch_task_id: uuid.UUID | None
    updated_at: datetime
    worker_name: str | None = None
    placement_human_id: str | None = None


class PlacementPanelOut(BaseModel):
    placement_id: uuid.UUID
    eligible: bool
    reason: str | None
    installation_id: uuid.UUID | None
    connector_key: str | None
    record: HrmsRecordOut | None
    placement_status: str


class ImportFileOut(BaseModel):
    file_id: uuid.UUID
    filename: str
    rows: int
    url: str | None = None


class JobQueuedOut(BaseModel):
    job_id: uuid.UUID
    status: str
