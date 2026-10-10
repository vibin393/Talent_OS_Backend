"""Leave & Holidays DTOs, internal app (Epic 26, FR-134..FR-136). Days are decimal strings (``DecimalStr``
out;
pattern-checked ``str`` in -- a JSON number is refused with 422, never coerced). Portal schemas live in
``schemas/leave_portal.py``."""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.core.money import DecimalStr

DaysIn = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{1,3}(\.\d{1,2})?$")]
SignedDaysIn = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^-?\d{1,3}(\.\d{1,2})?$")]
ScopeKind = Literal["country", "state", "client", "legal_entity"]
HolidayType = Literal["national", "state", "optional", "client"]
AccrualMode = Literal["monthly", "annual_grant", "none"]
LeaveStatus = Literal["requested", "approved", "rejected", "withdrawn", "cancellation_requested", "cancelled"]
LeaveClientMode = Literal["none", "notify", "approve"]


# ---------------------------------------------------------------- settings
class LeaveSettingsOut(BaseModel):
    leave_year: Literal["calendar", "fiscal"]
    default_work_week: list[int]
    accrual_day: int
    client_team_calendar_default: bool


class LeaveSettingsUpdate(BaseModel):
    leave_year: Literal["calendar", "fiscal"] | None = None
    default_work_week: list[Annotated[int, Field(ge=0, le=6)]] | None = None
    accrual_day: Annotated[int, Field(ge=1, le=28)] | None = None
    client_team_calendar_default: bool | None = None


class LeaveClientSettingsOut(BaseModel):
    client_id: uuid.UUID
    leave_client_mode: LeaveClientMode
    client_team_calendar: bool | None
    team_calendar_enabled: bool


class LeaveClientSettingsIn(BaseModel):
    leave_client_mode: LeaveClientMode = "none"
    client_team_calendar: bool | None = None


# ---------------------------------------------------------------- holiday calendars
class HolidayIn(BaseModel):
    holiday_date: date
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    type: HolidayType = "national"


class HolidayUpdate(BaseModel):
    holiday_date: date | None = None
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    type: HolidayType | None = None


class HolidayOut(BaseModel):
    id: uuid.UUID
    calendar_id: uuid.UUID
    holiday_date: date
    name: str
    type: HolidayType


class HolidayCalendarIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    year: Annotated[int, Field(ge=2000, le=2100)]
    scope_kind: ScopeKind
    country_code: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] = "IN"
    state_code: Annotated[str, StringConstraints(pattern=r"^\d{2}$")] | None = None
    client_id: uuid.UUID | None = None
    legal_entity_id: uuid.UUID | None = None
    optional_allowance: Annotated[int, Field(ge=0, le=366)] = 0
    holidays: list[HolidayIn] = Field(default_factory=list)


class HolidayCalendarUpdate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    optional_allowance: Annotated[int, Field(ge=0, le=366)] | None = None


class HolidayCalendarOut(BaseModel):
    id: uuid.UUID
    name: str
    year: int
    scope_kind: ScopeKind
    country_code: str
    state_code: str | None
    client_id: uuid.UUID | None
    legal_entity_id: uuid.UUID | None
    optional_allowance: int
    copied_from_id: uuid.UUID | None
    archived_at: datetime | None
    holiday_count: int = 0


class HolidayCalendarDetail(HolidayCalendarOut):
    holidays: list[HolidayOut]


class CopyCalendarIn(BaseModel):
    year: Annotated[int, Field(ge=2000, le=2100)]


class CopiedHoliday(BaseModel):
    id: uuid.UUID
    holiday_date: date
    name: str
    type: HolidayType
    needs_review: bool


class SkippedHoliday(BaseModel):
    holiday_date: date
    name: str
    reason: str


class CopyCalendarOut(BaseModel):
    calendar: HolidayCalendarOut
    copied: list[CopiedHoliday]
    skipped: list[SkippedHoliday]


class HolidayDayOut(BaseModel):
    date: date
    name: str
    type: HolidayType
    holiday_id: uuid.UUID
    calendar_id: uuid.UUID
    optional: bool = False
    selected: bool = False


class PlacementHolidaysOut(BaseModel):
    placement_id: uuid.UUID
    calendar_ids: list[uuid.UUID]
    holidays: list[HolidayDayOut]


# ---------------------------------------------------------------- leave types
class LeaveTypeIn(BaseModel):
    code: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_-]{1,20}$")]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    paid: bool = True
    billable: bool = False
    accrual_mode: AccrualMode = "none"
    accrual_amount: DaysIn = "0"
    cap: DaysIn | None = None
    carry_forward_limit: DaysIn | None = None
    allow_negative: bool = False
    negative_limit: DaysIn | None = None
    allow_half_day: bool = True
    eligible_worker_types: list[str] = Field(default_factory=list)
    eligible_legal_entity_ids: list[uuid.UUID] = Field(default_factory=list)


class LeaveTypeUpdate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    paid: bool | None = None
    billable: bool | None = None
    accrual_mode: AccrualMode | None = None
    accrual_amount: DaysIn | None = None
    cap: DaysIn | None = None
    carry_forward_limit: DaysIn | None = None
    allow_negative: bool | None = None
    negative_limit: DaysIn | None = None
    allow_half_day: bool | None = None
    eligible_worker_types: list[str] | None = None
    eligible_legal_entity_ids: list[uuid.UUID] | None = None
    archived: bool | None = None
    clear: list[Literal["cap", "carry_forward_limit", "negative_limit"]] = Field(default_factory=list)


class LeaveTypeOverrideIn(BaseModel):
    accrual_amount: DaysIn | None = None
    cap: DaysIn | None = None
    billable: bool | None = None
    requires_client_approval: bool | None = None


class LeaveTypeOverrideOut(BaseModel):
    id: uuid.UUID
    leave_type_id: uuid.UUID
    client_id: uuid.UUID
    accrual_amount: DecimalStr | None
    cap: DecimalStr | None
    billable: bool | None
    requires_client_approval: bool | None


class LeaveTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    paid: bool
    billable: bool
    accrual_mode: AccrualMode
    accrual_amount: DecimalStr
    cap: DecimalStr | None
    carry_forward_limit: DecimalStr | None
    allow_negative: bool
    negative_limit: DecimalStr | None
    allow_half_day: bool
    eligible_worker_types: list[str]
    eligible_legal_entity_ids: list[uuid.UUID]
    timesheet_category: str
    archived_at: datetime | None
    overrides: list[LeaveTypeOverrideOut] = Field(default_factory=list)


# ---------------------------------------------------------------- balances
class LeaveBalanceOut(BaseModel):
    candidate_id: uuid.UUID
    leave_type_id: uuid.UUID
    leave_type_code: str
    leave_type_name: str
    legal_entity_id: uuid.UUID
    leave_year: str
    balance: DecimalStr
    pending: DecimalStr
    available: DecimalStr
    paid: bool


class LeaveLedgerEntryOut(BaseModel):
    id: uuid.UUID
    leave_type_id: uuid.UUID
    legal_entity_id: uuid.UUID
    leave_year: str
    entry_kind: str
    days: DecimalStr
    effective_date: date
    source_ref: str
    reason: str | None
    actor_kind: str
    created_at: datetime


class AdjustBalanceIn(BaseModel):
    candidate_id: uuid.UUID
    leave_type_id: uuid.UUID
    legal_entity_id: uuid.UUID
    days: SignedDaysIn
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    leave_year: str | None = None


class VerifyBalancesOut(BaseModel):
    checked: int
    mismatches: list[dict[str, str]]


class AccrualRunIn(BaseModel):
    month: date


class AccrualRunOut(BaseModel):
    month: str
    posted: int


# ---------------------------------------------------------------- requests
class LeaveRequestIn(BaseModel):
    placement_id: uuid.UUID | None = None
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    start_half: Literal["pm"] | None = None
    end_half: Literal["am"] | None = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


class ProxyLeaveRequestIn(LeaveRequestIn):
    candidate_id: uuid.UUID
    proxy_reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class LeavePreviewOut(BaseModel):
    days: DecimalStr
    working_dates: list[date]
    holidays: list[HolidayDayOut]
    balance: DecimalStr | None
    pending: DecimalStr | None
    available: DecimalStr | None
    sufficient: bool
    error_code: str | None = None


class LeaveRequestOut(BaseModel):
    id: uuid.UUID
    human_id: str
    candidate_id: uuid.UUID
    worker_name: str | None = None
    placement_id: uuid.UUID
    client_id: uuid.UUID
    client_name: str | None = None
    legal_entity_id: uuid.UUID
    leave_type_id: uuid.UUID
    leave_type_name: str | None = None
    leave_year: str
    start_date: date
    end_date: date
    start_half: str | None
    end_half: str | None
    days: DecimalStr
    reason: str | None
    status: LeaveStatus
    approval_request_id: uuid.UUID | None
    requested_by_kind: str
    proxy_reason: str | None
    decided_at: datetime | None
    decision_comment: str | None
    created_at: datetime


class CommentIn(BaseModel):
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


# ---------------------------------------------------------------- team calendar
class LeaveCalendarDay(BaseModel):
    date: date
    half: Literal["am", "pm"] | None = None
    status: Literal["requested", "approved"]
    leave_type_name: str | None = None


class LeaveCalendarWorker(BaseModel):
    candidate_id: uuid.UUID
    worker_name: str | None
    placement_id: uuid.UUID
    client_id: uuid.UUID
    days: list[LeaveCalendarDay]


class LeaveCalendarOut(BaseModel):
    start: date
    end: date
    workers: list[LeaveCalendarWorker]
    holidays: list[HolidayDayOut]
