"""Time contract DTOs (Epic 13 contract layer; filled by Epic 25). Frozen; Decimals as strings.

Epic 25 adds the internal Timesheets API schemas and the read contracts used by Epics 26, 28, 31 and 34
(``TimesheetState``, ``UnapprovedTimesheet``, ``MonitorOut``, ``PrefillItem``). Worker / Client portal schemas
live in ``schemas/time_portal.py``.
"""

import dataclasses
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import DecimalStr


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class TimeBillingInputs(_Frozen):
    config_version: int
    holiday_calendar_id: uuid.UUID | None
    billable_categories: list[str]
    bill_multipliers: dict[str, DecimalStr]
    daily_cap_hours: DecimalStr | None
    hours_per_day: DecimalStr | None
    working_days: int
    month_working_days: int
    proration_factor: DecimalStr  # 6 dp, from time/calendar.py


class TimePayInputs(_Frozen):
    config_version: int
    holiday_calendar_id: uuid.UUID | None
    pay_multipliers: dict[str, DecimalStr]
    hours_per_day: DecimalStr | None
    working_days: int
    month_working_days: int
    proration_factor: DecimalStr
    # monthly segment pay rate / (month_working_days x hours_per_day), unrounded; None for non-monthly rates
    hourly_equivalent: DecimalStr | None


# ================================================================ Epic 25: Timesheets
TIME_CATEGORIES: tuple[str, ...] = ("regular", "ot", "double_time", "holiday", "leave_paid", "leave_unpaid")
COMPUTED_CATEGORIES: frozenset[str] = frozenset({"ot", "double_time"})
PERIOD_KINDS: tuple[str, ...] = ("weekly", "biweekly", "semimonthly", "monthly")
ENTRY_MODES: tuple[str, ...] = ("hours", "start_end", "days")
ROUTE_STEP_KINDS: tuple[str, ...] = (
    "placement_account_manager",
    "user",
    "client_user",
    "client_any",
    "finance_role",
)
TIMESHEET_STATUSES: tuple[str, ...] = ("draft", "submitted", "in_approval", "approved", "rejected")

TimeCategory = Literal["regular", "ot", "double_time", "holiday", "leave_paid", "leave_unpaid"]
PeriodKind = Literal["weekly", "biweekly", "semimonthly", "monthly"]
EntryMode = Literal["hours", "start_end", "days"]
RouteStepKind = Literal["placement_account_manager", "user", "client_user", "client_any", "finance_role"]
TimesheetStatus = Literal["draft", "submitted", "in_approval", "approved", "rejected"]
_Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
_Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RouteStep(BaseModel):
    """One approval route step. ``ref``: user id (``user``), client user id (``client_user``) or role key
    (``finance_role``, default ``finance_admin``); unused for ``placement_account_manager`` /
    ``client_any``."""

    kind: RouteStepKind
    ref: str | None = None
    label: str | None = None


def _all_categories() -> list[TimeCategory]:
    return ["regular", "ot", "double_time", "holiday", "leave_paid", "leave_unpaid"]


def _work_days() -> list[int]:
    return [0, 1, 2, 3, 4]


class TimeConfigValues(BaseModel):
    """Values shared by Client defaults and Placement configs (FR-128). Decimals serialise as strings."""

    period_kind: PeriodKind = "weekly"
    week_start: int = Field(0, ge=0, le=6)  # 0 = Monday
    entry_mode: EntryMode = "hours"
    hours_per_day: DecimalStr = Decimal("8.00")
    work_days: list[int] = Field(default_factory=_work_days)  # 0 = Monday (Epic 26 leave day counts)
    enabled_categories: list[TimeCategory] = Field(default_factory=_all_categories)
    ot_daily_threshold: DecimalStr | None = None
    dt_daily_threshold: DecimalStr | None = None
    ot_weekly_threshold: DecimalStr | None = None
    pay_multipliers: dict[str, DecimalStr] = Field(default_factory=dict)
    bill_multipliers: dict[str, DecimalStr] = Field(default_factory=dict)
    daily_cap: DecimalStr = Decimal("24.00")
    prefill_holidays: bool = True
    prefill_leave: bool = True
    holiday_calendar_id: uuid.UUID | None = None
    approval_route: list[RouteStep] = Field(default_factory=list)


class TimeConfigIn(TimeConfigValues):
    model_config = ConfigDict(extra="forbid")


class ClientTimeDefaultsOut(TimeConfigValues):
    client_id: uuid.UUID
    is_default: bool  # True when the Client has no row yet (tenant defaults shown)
    updated_at: datetime | None = None


class TimesheetConfigOut(TimeConfigValues):
    id: uuid.UUID
    placement_id: uuid.UUID
    version: int
    effective_period_start: date
    created_at: datetime


class TimeSettingsOut(BaseModel):
    missing_grace_days: int
    reminder_offsets: list[int]
    copy_previous_enabled: bool
    import_allow_client_approved: bool
    allow_empty_route: bool


class TimeSettingsUpdate(_In):
    missing_grace_days: int | None = Field(None, ge=0, le=60)
    reminder_offsets: list[int] | None = None
    copy_previous_enabled: bool | None = None
    import_allow_client_approved: bool | None = None
    allow_empty_route: bool | None = None


# ---------------------------------------------------------------- entries
class SpanIn(_In):
    start: str  # "HH:MM"
    end: str  # "HH:MM"
    break_minutes: int = Field(0, ge=0, le=1440)


class DayEntryIn(_In):
    """One (date, category) cell: ``hours`` (hours mode; a string with <= 2 dp), ``spans`` (start_end mode) or
    ``days`` (days mode). Empty / ``"0"`` deletes the cell. ``ot`` / ``double_time`` are computed, never
    entered."""

    date: date
    category: TimeCategory
    hours: str | None = None
    spans: list[SpanIn] | None = None
    days: str | None = None
    note: _Note | None = None


class EntriesIn(_In):
    days: list[DayEntryIn] = Field(default_factory=list, max_length=400)


class ProxyEntriesIn(EntriesIn):
    reason: _Reason


class SubmitIn(_In):
    confirm_zero: bool = False


class ProxySubmitIn(SubmitIn):
    reason: _Reason


class ReasonIn(_In):
    reason: _Reason


class AdjustmentEntryIn(_In):
    date: date
    category: TimeCategory
    delta_hours: str  # signed, <= 2 dp
    note: _Note | None = None


class AdjustmentIn(_In):
    reason: _Reason
    entries: list[AdjustmentEntryIn] = Field(min_length=1, max_length=400)


class EntryOut(BaseModel):
    id: uuid.UUID
    entry_date: date
    category: str
    hours: DecimalStr
    entered_hours: DecimalStr | None
    start_time: str | None
    end_time: str | None
    break_minutes: int | None
    prefilled: bool
    is_proxy: bool
    holiday_id: uuid.UUID | None
    leave_type_id: uuid.UUID | None
    note: str | None


class AdjustmentEntryOut(BaseModel):
    id: uuid.UUID
    entry_date: date
    category: str
    delta_hours: DecimalStr
    note: str | None


class CategoryTotal(BaseModel):
    category: str
    hours: DecimalStr


class DaySplitOut(BaseModel):
    date: date
    worked: DecimalStr
    regular: DecimalStr
    ot: DecimalStr
    double_time: DecimalStr


class SplitPreviewOut(BaseModel):
    days: list[DaySplitOut]
    totals: list[CategoryTotal]


class LockFlags(BaseModel):
    payroll_locked: bool
    billing_locked: bool
    in_draft_invoice: bool


class ApprovalStepOut(BaseModel):
    position: int
    name: str
    outcome: str | None
    actor_kind: str | None
    comment: str | None
    decided_at: datetime | None


class TimesheetSummaryOut(BaseModel):
    id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    placement_human_id: str | None = None
    candidate_id: uuid.UUID
    worker_name: str | None = None
    client_id: uuid.UUID
    client_name: str | None = None
    kind: str
    adjustment_seq: int
    original_timesheet_id: uuid.UUID | None
    period_start: date
    period_end: date
    status: TimesheetStatus
    total_hours: DecimalStr
    submitted_at: datetime | None
    approved_at: datetime | None


class TimesheetDetailOut(TimesheetSummaryOut):
    config_version: int
    entry_mode: str
    hours_per_day: DecimalStr
    daily_cap: DecimalStr
    enabled_categories: list[str]
    entries: list[EntryOut]
    adjustment_entries: list[AdjustmentEntryOut]
    totals: list[CategoryTotal]
    locks: LockFlags
    approval_request_id: uuid.UUID | None
    current_step: str | None
    approval_history: list[ApprovalStepOut]
    rejection_comment: str | None
    proxy_reason: str | None
    reason: str | None
    client_approval_evidence_file_id: uuid.UUID | None


# ---------------------------------------------------------------- monitor (FR-133)
class MonitorRow(BaseModel):
    timesheet_id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    placement_human_id: str | None
    candidate_id: uuid.UUID
    worker_name: str | None
    client_id: uuid.UUID
    client_name: str | None
    legal_entity_id: uuid.UUID
    kind: str
    period_start: date
    period_end: date
    status: str
    current_step: str | None
    current_approver: str | None
    age_business_days: int
    missing: bool
    late: bool
    total_hours: DecimalStr
    currency: str
    estimated_unbilled_value: DecimalStr | None  # None when masked (no compensation:view) or not computable


class CurrencyTotal(BaseModel):
    currency: str
    estimated_unbilled_value: DecimalStr | None


class MonitorOut(BaseModel):
    rows: list[MonitorRow]
    totals: list[CurrencyTotal]  # per currency, never summed across currencies
    masked: bool


class RemindIn(_In):
    timesheet_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class RemindSkip(BaseModel):
    timesheet_id: uuid.UUID
    code: str


class RemindOut(BaseModel):
    sent: list[uuid.UUID]
    skipped: list[RemindSkip]


# ---------------------------------------------------------------- import (FR-131)
class ImportRowOut(BaseModel):
    row: int
    worker: str | None
    placement_id: uuid.UUID | None
    date: date | None
    category: str | None
    hours: str | None
    timesheet_id: uuid.UUID | None
    errors: list[str]


class ImportPreviewOut(BaseModel):
    batch_id: uuid.UUID
    status: str
    row_count: int
    error_count: int
    mark_client_approved: bool
    rows: list[ImportRowOut]


class ImportApplyOut(BaseModel):
    batch_id: uuid.UUID
    applied_rows: int
    submitted_timesheet_ids: list[uuid.UUID]


# ---------------------------------------------------------------- read contracts (Epics 26, 28, 31, 34)
@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetState:
    """``time_service.timesheet_state_for`` (Epic 26 leave guard): the regular Timesheet covering a date.
    ``locked`` = an entry holds a non-released posting in either ledger."""

    timesheet_id: uuid.UUID
    status: str
    locked: bool


class UnapprovedTimesheet(BaseModel):
    """``time_service.unapproved_timesheets`` (Epic 28 payroll exceptions)."""

    timesheet_id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    legal_entity_id: uuid.UUID
    kind: str
    period_start: date
    period_end: date
    status: str


@dataclasses.dataclass(frozen=True, slots=True)
class PrefillItem:
    """One prefilled cell from a registered prefill source (Epic 26 registers ``holidays`` and
    ``approved_leave``). ``deletable``: the Worker may remove the row while the Timesheet is a draft."""

    date: date
    category: str
    hours: Decimal
    holiday_id: uuid.UUID | None = None
    leave_type_id: uuid.UUID | None = None
    label: str | None = None
    deletable: bool = False
