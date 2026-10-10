"""Leave portal DTOs (Epic 26, AD-26). Worker schemas show the Worker's own requests, balances and holidays;
the Client schema shows only Worker name, dates, half-day marks and "On leave" -- never the leave type, the
reason or balances (field-visibility guard)."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.field_visibility import register_portal_schema
from app.core.money import DecimalStr


@register_portal_schema("worker")
class WorkerLeaveTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    paid: bool
    allow_half_day: bool


@register_portal_schema("worker")
class WorkerLeaveBalanceOut(BaseModel):
    leave_type_id: uuid.UUID
    leave_type_name: str
    leave_year: str
    balance: DecimalStr
    pending: DecimalStr
    available: DecimalStr


@register_portal_schema("worker")
class WorkerLeaveRequestOut(BaseModel):
    id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    leave_type_id: uuid.UUID
    leave_type_name: str | None
    start_date: date
    end_date: date
    start_half: str | None
    end_half: str | None
    days: DecimalStr
    reason: str | None
    status: str
    decided_at: datetime | None
    decision_comment: str | None
    created_at: datetime


@register_portal_schema("worker")
class WorkerLeavePreviewOut(BaseModel):
    days: DecimalStr
    working_dates: list[date]
    available: DecimalStr | None
    sufficient: bool
    error_code: str | None = None


@register_portal_schema("worker")
class WorkerHolidayOut(BaseModel):
    holiday_id: uuid.UUID
    date: date
    name: str
    type: str
    optional: bool
    selected: bool


@register_portal_schema("worker")
class WorkerHolidaysOut(BaseModel):
    year: int
    placement_id: uuid.UUID | None
    optional_allowance: int
    optional_selected: int
    holidays: list[WorkerHolidayOut]


class WorkerOptionalHolidayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holiday_id: uuid.UUID
    placement_id: uuid.UUID | None = None
    selected: bool = True


@register_portal_schema("client_user")
class ClientLeaveCalendarDay(BaseModel):
    date: date
    half: Literal["am", "pm"] | None = None
    label: Literal["On leave"] = "On leave"


@register_portal_schema("client_user")
class ClientLeaveCalendarRow(BaseModel):
    worker_name: str | None
    placement_id: uuid.UUID
    days: list[ClientLeaveCalendarDay]


@register_portal_schema("client_user")
class ClientLeaveCalendarOut(BaseModel):
    start: date
    end: date
    rows: list[ClientLeaveCalendarRow]
