"""Timesheet portal DTOs (Epic 25, AD-26 / AD-27, FR-80).

* Worker ("My Work") schemas: hours only -- no bill rate, vendor rate or margin.
* Client schemas: hours only -- no pay / bill / vendor rate, amounts, margin, vendor identity or free-text
  ``note`` fields (the Worker's day comment is exposed as ``day_comment``).

Both are registered with the field-visibility guard; Worker and Client schemas are separate classes.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.field_visibility import register_portal_schema
from app.core.money import DecimalStr


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ Worker
@register_portal_schema("worker")
class WorkerTimesheetSummary(BaseModel):
    id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    client_name: str | None
    role_title: str | None
    kind: str
    period_start: date
    period_end: date
    status: str
    total_hours: DecimalStr
    submitted_at: datetime | None


@register_portal_schema("worker")
class WorkerEntry(BaseModel):
    id: uuid.UUID
    entry_date: date
    category: str
    hours: DecimalStr
    entered_hours: DecimalStr | None
    start_time: str | None
    end_time: str | None
    break_minutes: int | None
    prefilled: bool
    deletable: bool
    holiday_id: uuid.UUID | None
    leave_type_id: uuid.UUID | None
    note: str | None


@register_portal_schema("worker")
class WorkerCategoryTotal(BaseModel):
    category: str
    hours: DecimalStr


@register_portal_schema("worker")
class WorkerAdjustmentEntry(BaseModel):
    entry_date: date
    category: str
    delta_hours: DecimalStr
    note: str | None


@register_portal_schema("worker")
class WorkerTimesheetOut(WorkerTimesheetSummary):
    entry_mode: str
    hours_per_day: DecimalStr
    daily_cap: DecimalStr
    enabled_categories: list[str]
    editable_categories: list[str]
    editable: bool
    can_recall: bool
    can_copy_previous: bool
    min_date: date
    max_date: date
    entries: list[WorkerEntry]
    adjustment_entries: list[WorkerAdjustmentEntry]
    totals: list[WorkerCategoryTotal]
    rejection_comment: str | None


@register_portal_schema("worker")
class WorkerDaySplit(BaseModel):
    date: date
    worked: DecimalStr
    regular: DecimalStr
    ot: DecimalStr
    double_time: DecimalStr


@register_portal_schema("worker")
class WorkerSplitPreview(BaseModel):
    days: list[WorkerDaySplit]
    totals: list[WorkerCategoryTotal]


# ================================================================ Client
@register_portal_schema("client_user")
class ClientTimesheetFlags(BaseModel):
    ot_above_threshold: bool
    holiday_entries: bool
    above_daily_cap: bool


@register_portal_schema("client_user")
class ClientTimesheetSummary(BaseModel):
    id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    worker_name: str | None
    role_title: str | None
    kind: str
    period_start: date
    period_end: date
    status: str
    total_hours: DecimalStr
    totals: dict[str, DecimalStr]
    flags: ClientTimesheetFlags
    approval_request_id: uuid.UUID | None
    awaiting_client: bool  # the current step is a Client step this user may decide
    submitted_at: datetime | None


@register_portal_schema("client_user")
class ClientTimesheetDay(BaseModel):
    date: date
    hours: dict[str, DecimalStr]  # category -> hours
    total: DecimalStr
    day_comment: str | None


@register_portal_schema("client_user")
class ClientTimesheetDetail(ClientTimesheetSummary):
    days: list[ClientTimesheetDay]
    adjustment_deltas: list[ClientTimesheetDay]
    rejection_comment: str | None


class ClientDecideIn(_In):
    action: Literal["approve", "reject"]
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] | None = None


class ClientBulkPreviewIn(_In):
    timesheet_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


@register_portal_schema("client_user")
class ClientBulkPreviewOut(BaseModel):
    items: list[ClientTimesheetSummary]
    not_decidable: list[uuid.UUID]
    total_hours: DecimalStr
    preview_hash: str


class ClientBulkDecideIn(_In):
    timesheet_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    action: Literal["approve", "reject"]
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] | None = None
    preview_hash: str


@register_portal_schema("client_user")
class ClientBulkSkip(BaseModel):
    timesheet_id: uuid.UUID
    code: str


@register_portal_schema("client_user")
class ClientBulkDecideOut(BaseModel):
    decided: list[uuid.UUID]
    skipped: list[ClientBulkSkip]
