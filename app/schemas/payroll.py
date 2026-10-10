"""Payroll DTOs: the Epic 13 contract (``PayPeriodRef``) and the Epic 28 Payroll Export API."""

import uuid
import uuid as _uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import DecimalStr


class PayPeriodRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    legal_entity_id: uuid.UUID
    start_date: date
    end_date: date
    status: str


# ================================================================ Epic 28: Payroll Export (FR-140..FR-142)
# Decimals are strings on the wire (AD-12). No statutory amounts anywhere (PRD OQ2).
LateItemMapping = Literal["item_date", "approval_date"]
PayrollWorkerType = Literal["employee", "contractor"]
PeriodStatus = Literal["open", "locked"]
VersionKind = Literal["preview", "final", "supplementary"]
VersionStatus = Literal["building", "generated", "finalized", "processed", "superseded"]
FileFormat = Literal["csv", "xlsx"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PayrollSettingsOut(BaseModel):
    late_item_mapping: LateItemMapping
    include_worker_types: list[PayrollWorkerType]


class PayrollSettingsUpdate(_In):
    late_item_mapping: LateItemMapping | None = None
    include_worker_types: list[PayrollWorkerType] | None = None


class PayCalendarOut(BaseModel):
    id: _uuid.UUID | None
    legal_entity_id: _uuid.UUID
    frequency: Literal["monthly"]
    cutoff_day: int | Literal["last"]
    active: bool
    timezone: str


class PayCalendarIn(_In):
    cutoff_day: Annotated[int, Field(ge=1, le=28)] | Literal["last"] = "last"
    active: bool = True
    generate_from: date | None = None


class PayPeriodOut(BaseModel):
    id: _uuid.UUID
    human_id: str
    legal_entity_id: _uuid.UUID
    period_start: date
    period_end: date
    status: PeriodStatus
    locked_at: datetime | None
    reopened_count: int
    last_reopen_reason: str | None
    final_count: int = 0
    supplementary_count: int = 0
    processed: bool = False


class ReopenIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=1000)]


class ExceptionItem(BaseModel):
    kind: str
    candidate_id: _uuid.UUID | None = None
    placement_id: _uuid.UUID | None = None
    source_kind: str | None = None
    source_id: _uuid.UUID | None = None
    timesheet_id: _uuid.UUID | None = None
    label: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ExceptionsOut(BaseModel):
    pay_period_id: _uuid.UUID
    blocking: bool
    counts: dict[str, int]
    unapproved_timesheets: list[ExceptionItem]
    missing_pay_rate: list[ExceptionItem]
    missing_bank_details: list[ExceptionItem]
    currency_mismatch: list[ExceptionItem]


class PreviewIn(_In):
    exclude_exceptions: bool = False


class VersionOut(BaseModel):
    id: _uuid.UUID
    human_id: str
    pay_period_id: _uuid.UUID
    legal_entity_id: _uuid.UUID
    kind: VersionKind
    supplementary: bool
    seq: int
    status: VersionStatus
    based_on_preview_id: _uuid.UUID | None
    exclude_exceptions: bool
    generated_at: datetime | None
    finalized_at: datetime | None
    items_hash: str | None
    content_hash: str | None
    exceptions: dict[str, Any]
    totals: dict[str, Any]
    row_count: int
    item_count: int
    provider_status: str
    provider_reference: str | None
    processed_at: datetime | None


class RowOut(BaseModel):
    id: _uuid.UUID
    candidate_id: _uuid.UUID
    worker_human_id: str | None
    worker_name: str | None = None
    placement_ids: list[_uuid.UUID]
    worker_type: str | None
    pan_last4: str | None
    bank_last4: str | None
    ifsc: str | None
    currency: str
    hours_regular: DecimalStr
    hours_ot: DecimalStr
    hours_double_time: DecimalStr
    hours_holiday: DecimalStr
    hours_leave_paid: DecimalStr
    hours_leave_unpaid: DecimalStr
    days_leave_paid: DecimalStr
    days_leave_unpaid: DecimalStr
    earnings_regular: DecimalStr
    earnings_ot: DecimalStr
    earnings_double_time: DecimalStr
    earnings_holiday: DecimalStr
    earnings_leave_paid: DecimalStr
    earnings_base: DecimalStr
    pay_rates: dict[str, Any]
    reimbursements_total: DecimalStr
    adjustments_total: DecimalStr
    rate_adjustments_total: DecimalStr
    gross_earnings_inputs: DecimalStr


class ItemOut(BaseModel):
    id: _uuid.UUID
    source_kind: str
    source_id: _uuid.UUID | None
    placement_id: _uuid.UUID | None
    bucket: str
    category: str | None
    item_date: date
    quantity: DecimalStr
    rate: DecimalStr | None
    multiplier: DecimalStr | None
    amount: DecimalStr
    currency: str


class MarkProcessedIn(_In):
    payroll_reference: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class MappingColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_field: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    header: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    include: bool = True


class MappingIn(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    legal_entity_id: _uuid.UUID | None = None
    format: FileFormat = "csv"
    columns: list[MappingColumn]
    is_default: bool = False


class MappingUpdate(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] | None = None
    format: FileFormat | None = None
    columns: list[MappingColumn] | None = None
    is_default: bool | None = None


class MappingOut(BaseModel):
    id: _uuid.UUID
    legal_entity_id: _uuid.UUID | None
    name: str
    format: FileFormat
    columns: list[MappingColumn]
    is_default: bool


class CatalogueField(BaseModel):
    key: str
    label: str
    identity: bool = False
    placeholder: bool = False


class PayrollConnectorOut(BaseModel):
    legal_entity_id: _uuid.UUID
    provider_key: str
    config_id: _uuid.UUID | None


class PayrollConnectorIn(_In):
    provider_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
