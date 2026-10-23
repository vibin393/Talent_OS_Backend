# ruff: file-ignore[line-too-long]
"""Workforce planning API schemas (Epic 44, FR-222..FR-225).

Money is ``Decimal`` end to end (AD-14 / AD-20): inputs accept a decimal string or an integer -- a JSON float is
refused (``float_money``) -- and are quantised to 2 dp; outputs serialise as strings (``DecimalStr``). Money fields
are ``None`` with ``money_masked = true`` for a viewer without ``compensation:view``.
``PlanLineImportIn`` is the go-live import row (foundation 0.7, FR-263): field names are the CSV column names.
"""

import re
import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.core.money import DecimalStr

PlanStatus = Literal["draft", "budgeted", "pending_approval", "approved", "rejected", "revision_draft"]
PlanOrigin = Literal["internal", "client_forecast"]
LineType = Literal["new", "replacement", "project", "recurring"]
WorkMode = Literal["onsite", "remote", "hybrid"]
LineRateUnit = Literal["hour", "day", "month", "annual"]
ChangeKind = Literal["unchanged", "added", "modified", "removed"]
SlotStatus = Literal["open", "requisitioned", "filled", "cancelled"]
GroupBy = Literal["line", "month"]

Str200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Str300 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
Text4k = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
MONEY_MAX = Decimal("9999999999999999.99")


def parse_money(value: Any) -> Decimal | None:
    """Decimal money (2 dp). A float is refused -- money never travels as a binary float (AD-20)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool | float):
        msg = 'Money must be a decimal string (e.g. "1200.50"), not a floating-point number'
        raise ValueError(msg)
    if isinstance(value, int):
        value = Decimal(value)
    if isinstance(value, str):
        try:
            value = Decimal(value.strip().replace(",", ""))
        except InvalidOperation:
            msg = "Not a decimal amount"
            raise ValueError(msg) from None
    if not isinstance(value, Decimal) or not value.is_finite():
        msg = "Not a decimal amount"
        raise ValueError(msg)
    if value < 0:
        msg = "Money cannot be negative"
        raise ValueError(msg)
    if value != value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
        msg = "Money has at most 2 decimal places"
        raise ValueError(msg)
    if value > MONEY_MAX:
        msg = "Amount too large"
        raise ValueError(msg)
    return value.quantize(Decimal("0.01"))


MoneyIn = Annotated[Decimal | None, BeforeValidator(parse_money)]


def parse_month(value: Any) -> date:
    """``YYYY-MM`` (or a date) -> the first day of that month."""
    if isinstance(value, date):
        return value.replace(day=1)
    match = MONTH_RE.match(str(value or "").strip())
    if match is None:
        msg = "Use a month as YYYY-MM"
        raise ValueError(msg)
    return date(int(match.group(1)), int(match.group(2)), 1)


Month = Annotated[date, BeforeValidator(parse_month)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ plans
class PlanCreate(_In):
    name: Str200
    period_start: date
    period_end: date
    business_unit_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    notes: Text4k | None = None

    @model_validator(mode="after")
    def _period(self) -> "PlanCreate":
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        return self


class PlanUpdate(_In):
    name: Str200 | None = None
    period_start: date | None = None
    period_end: date | None = None
    business_unit_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    notes: Text4k | None = None


class PlanCopyIn(_In):
    name: Str200
    period_start: date


class RevisionStartIn(_In):
    reason: Text4k | None = None


class SlotCounts(BaseModel):
    open: int = 0
    requisitioned: int = 0
    filled: int = 0
    cancelled: int = 0


class PlanOut(BaseModel):
    id: uuid.UUID
    name: str
    period_start: date
    period_end: date
    fiscal_label: str | None
    business_unit_id: uuid.UUID | None
    client_id: uuid.UUID | None
    client_name: str | None = None
    project_id: uuid.UUID | None
    currency: str
    status: PlanStatus
    origin: PlanOrigin
    owner_id: uuid.UUID
    owner_name: str | None = None
    submitted_by_client_user_id: uuid.UUID | None
    approved_revision: int | None
    current_revision: int
    approval_request_id: uuid.UUID | None
    notes: str | None
    picked_up_at: datetime | None
    submitted_at: datetime | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    line_count: int = 0
    total_headcount: int = 0
    total_budget: DecimalStr | None = None
    money_masked: bool = False
    slots: SlotCounts = Field(default_factory=SlotCounts)
    editable: bool = False


# ================================================================ lines
class LineSkillIn(_In):
    """A line skill: ``skill_key`` (``global:<uuid>`` / ``tenant:<uuid>``) or a ``name`` matched to the catalogue."""

    skill_key: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=60)] | None
    ) = None
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    min_proficiency: Annotated[int, Field(ge=1, le=5)] | None = None

    @model_validator(mode="after")
    def _one(self) -> "LineSkillIn":
        if not self.skill_key and not self.name:
            raise ValueError("A skill needs a skill_key or a name")
        return self


class LineIn(_In):
    role_title: Str200
    skills: list[LineSkillIn] = Field(default_factory=list, max_length=50)
    headcount: int = Field(ge=1)
    line_type: LineType = "new"
    location_text: Str300 | None = None
    work_mode: WorkMode | None = None
    start_month: Month
    end_month: Month | None = None
    budget_amount: MoneyIn = None
    bill_rate_amount: MoneyIn = None
    rate_unit: LineRateUnit | None = None
    project_role_id: uuid.UUID | None = None
    notes: Text4k | None = None


class LineUpdate(_In):
    role_title: Str200 | None = None
    skills: list[LineSkillIn] | None = Field(default=None, max_length=50)
    headcount: int | None = Field(default=None, ge=1)
    line_type: LineType | None = None
    location_text: Str300 | None = None
    work_mode: WorkMode | None = None
    start_month: Month | None = None
    end_month: Month | None = None
    budget_amount: MoneyIn = None
    bill_rate_amount: MoneyIn = None
    rate_unit: LineRateUnit | None = None
    project_role_id: uuid.UUID | None = None
    notes: Text4k | None = None


class LineSkillOut(BaseModel):
    skill_key: str | None
    name: str
    min_proficiency: int | None


class LineOut(BaseModel):
    id: uuid.UUID
    lineage_id: uuid.UUID
    workforce_plan_id: uuid.UUID
    revision: int
    role_title: str
    skills: list[LineSkillOut]
    headcount: int
    line_type: LineType
    location_text: str | None
    work_mode: WorkMode | None
    start_month: str
    period_start: date
    period_end: date
    budget_amount: DecimalStr | None
    bill_rate_amount: DecimalStr | None
    rate_unit: LineRateUnit | None
    project_role_id: uuid.UUID | None
    status: str
    derived_status: str
    change_kind: ChangeKind
    supersedes_line_id: uuid.UUID | None
    notes: str | None
    client_comment: str | None
    slots: SlotCounts = Field(default_factory=SlotCounts)


class ImportRowError(BaseModel):
    row: int
    field: str | None
    message: str


class LineImportOut(BaseModel):
    batch_id: uuid.UUID | None
    applied: bool
    row_count: int
    valid_count: int
    error_count: int
    created: int
    errors: list[ImportRowError]


class PlanLineImportIn(_In):
    """Go-live / file import row: CSV column names (``start_month`` = ``YYYY-MM``; skills ``"Java:4; Spring:3"``)."""

    plan_name: str | None = None
    role: str
    skills: str | None = None
    headcount: str | int
    type: str | None = None
    location: str | None = None
    start_month: str
    budget: str | int | None = None
    bill_rate: str | int | None = None
    unit: str | None = None
    notes: str | None = None

    @field_validator("budget", "bill_rate", mode="before")
    @classmethod
    def _no_float(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Money must be a decimal string, not a floating-point number")
        return value


# ================================================================ revisions
class RevisionOut(BaseModel):
    id: uuid.UUID
    revision_no: int
    status: str
    reason: str | None
    created_by: uuid.UUID | None
    approval_request_id: uuid.UUID | None
    submitted_at: datetime | None
    decided_at: datetime | None
    created_at: datetime
    changed_lines: int


class DiffLine(BaseModel):
    lineage_id: uuid.UUID
    line_id: uuid.UUID
    change_kind: ChangeKind
    role_title: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    changed_fields: list[str]


class DiffOut(BaseModel):
    revision_no: int
    status: str
    money_masked: bool = False
    lines: list[DiffLine]


# ================================================================ slots
class SlotOut(BaseModel):
    id: uuid.UUID
    workforce_plan_id: uuid.UUID
    plan_line_id: uuid.UUID
    line_id: uuid.UUID | None
    role_title: str | None
    start_month: str | None
    seq: int
    status: SlotStatus
    demand_id: uuid.UUID | None
    demand_human_id: str | None = None
    demand_status: str | None = None
    placement_id: uuid.UUID | None
    converted_at: datetime | None
    requisitioned_at: datetime | None
    filled_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None


class ConvertOverrides(_In):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)] | None = (
        None
    )
    priority: Literal["low", "medium", "high", "critical"] | None = None
    target_closure_date: date | None = None
    start_date: date | None = None


class ConvertIn(_In):
    slot_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    client_id: uuid.UUID | None = None
    overrides: ConvertOverrides | None = None


class ConvertedSlot(BaseModel):
    slot_id: uuid.UUID
    demand_id: uuid.UUID
    demand_human_id: str | None


class ConvertOut(BaseModel):
    converted: list[ConvertedSlot]


class SlotCancelIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


# ================================================================ plan vs actual
class ActualRow(BaseModel):
    key: str
    line_id: uuid.UUID | None = None
    lineage_id: uuid.UUID | None = None
    role_title: str | None = None
    month: str | None = None
    planned: int = 0
    requisitioned: int = 0
    filled: int = 0
    open: int = 0
    cancelled: int = 0
    budget: DecimalStr | None = None
    committed: DecimalStr | None = None
    variance: DecimalStr | None = None
    pending_commitment: bool = False
    fx_missing: bool = False
    demand_ids: list[uuid.UUID] = Field(default_factory=list)


class ActualsOut(BaseModel):
    plan_id: uuid.UUID
    group_by: GroupBy
    currency: str
    money_masked: bool
    effective_revision: int | None
    rows: list[ActualRow]
    totals: ActualRow


# ================================================================ settings / summary
class WorkforceSettingsOut(BaseModel):
    standard_hours_per_month: DecimalStr
    standard_days_per_month: DecimalStr
    max_headcount_per_line: int


class WorkforceSettingsIn(_In):
    standard_hours_per_month: Decimal | None = Field(default=None, gt=0, le=744, decimal_places=2)
    standard_days_per_month: Decimal | None = Field(default=None, gt=0, le=31, decimal_places=2)
    max_headcount_per_line: int | None = Field(default=None, ge=1, le=500)


class PlanSummaryOut(BaseModel):
    """``workforce_service.plan_summary`` (Epic 52 dashboards): plans touching ``[from, to]``."""

    model_config = ConfigDict(frozen=True)

    period_from: date
    period_to: date
    plans_by_status: dict[str, int]
    approved_headcount: int
    open_slots: int
    requisitioned_slots: int
    filled_slots: int
    cancelled_slots: int


__all__ = [
    "ActualRow",
    "ActualsOut",
    "ConvertIn",
    "ConvertOut",
    "ConvertOverrides",
    "ConvertedSlot",
    "DiffLine",
    "DiffOut",
    "ImportRowError",
    "LineImportOut",
    "LineIn",
    "LineOut",
    "LineSkillIn",
    "LineSkillOut",
    "LineUpdate",
    "MoneyIn",
    "Month",
    "PlanCopyIn",
    "PlanCreate",
    "PlanLineImportIn",
    "PlanOut",
    "PlanSummaryOut",
    "PlanUpdate",
    "RevisionOut",
    "RevisionStartIn",
    "SlotCancelIn",
    "SlotCounts",
    "SlotOut",
    "WorkforceSettingsIn",
    "WorkforceSettingsOut",
    "parse_money",
    "parse_month",
]
