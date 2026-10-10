# ruff: noqa: E501
"""Receivables DTOs (Epic 13 contract layer; filled by Epic 30). ``open_balance`` returns a Decimal.

Money is a decimal string in JSON (AD-12). Collection activities never appear in portal schemas
(``schemas/receivables_portal.py``).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import DecimalStr

ReceivableDocumentKind = Literal["invoice", "debit_note"]
PaymentMode = Literal["neft", "rtgs", "imps", "upi", "cheque", "wire"]
TdsKind = Literal["income_tax", "gst_tds"]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=2)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- settings
class ReceivablesSettingsOut(_Out):
    aging_buckets: list[int]
    auto_allocate_order: str
    tds_variance_tolerance: DecimalStr
    ptp_grace_days: int
    reminder_rules_enabled: bool


class ReceivablesSettingsUpdate(_In):
    aging_buckets: Annotated[list[Annotated[int, Field(gt=0, le=3650)]], Field(min_length=1, max_length=8)] | None = None
    tds_variance_tolerance: Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=2)] | None = None
    ptp_grace_days: Annotated[int, Field(ge=0, le=90)] | None = None
    reminder_rules_enabled: bool | None = None


# ---------------------------------------------------------------- payments / allocations
class AllocationIn(_In):
    document_kind: ReceivableDocumentKind
    document_id: uuid.UUID
    amount: PositiveMoney


class TdsIn(_In):
    document_kind: ReceivableDocumentKind
    document_id: uuid.UUID
    kind: TdsKind = "income_tax"
    section_id: uuid.UUID | None = None
    rate_pct: Annotated[Decimal, Field(ge=0, le=100, max_digits=7, decimal_places=4)] | None = None
    amount: PositiveMoney


class PaymentIn(_In):
    legal_entity_id: uuid.UUID
    client_id: uuid.UUID
    payment_date: date
    amount: PositiveMoney
    currency: Annotated[str, StringConstraints(min_length=3, max_length=3, to_upper=True)] = "INR"
    fx_rate: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=6)] | None = None
    mode: PaymentMode
    reference: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    receiving_bank_account_id: uuid.UUID | None = None
    notes: Annotated[str, StringConstraints(max_length=2000)] | None = None
    allocations: Literal["auto", "none"] | list[AllocationIn] = "auto"
    tds: Annotated[list[TdsIn], Field(max_length=200)] = Field(default_factory=list)


class AllocateIn(_In):
    allocations: Annotated[list[AllocationIn], Field(min_length=1, max_length=500)]
    allocation_date: date | None = None


class ReverseIn(_In):
    reason: Reason
    reversal_date: date | None = None


class PaymentReverseIn(ReverseIn):
    reverse_tds: bool = True


class AllocationOut(_Out):
    id: uuid.UUID
    source_kind: str
    payment_id: uuid.UUID | None
    credit_note_id: uuid.UUID | None
    document_kind: str
    document_id: uuid.UUID
    document_number: str | None = None
    allocation_date: date
    amount: DecimalStr
    currency: str
    inr_at_document: DecimalStr
    inr_at_payment: DecimalStr
    realized_fx: DecimalStr
    prior_period: bool
    reversed: bool = False
    reversal_date: date | None = None


class TdsOut(_Out):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    client_id: uuid.UUID
    payment_id: uuid.UUID | None
    document_kind: str
    document_id: uuid.UUID
    document_number: str | None = None
    kind: str
    section_code: str | None
    rate_pct: DecimalStr
    base_amount: DecimalStr
    expected_amount: DecimalStr
    amount: DecimalStr
    currency: str
    variance_flag: bool
    deduction_date: date
    quarter: str
    certificate_status: str
    certificate_ref: str | None
    certificate_received_at: date | None
    reversed: bool = False


class PaymentOut(_Out):
    id: uuid.UUID
    human_id: str
    legal_entity_id: uuid.UUID
    client_id: uuid.UUID
    payment_date: date
    amount: DecimalStr
    currency: str
    fx_rate: DecimalStr | None
    fx_source: str | None
    inr_amount: DecimalStr
    mode: str
    reference: str | None
    receiving_bank_account_id: uuid.UUID | None
    allocation_status: str
    reversed_at: datetime | None
    reversal_reason: str | None
    reversal_date: date | None
    prior_period: bool
    unallocated: DecimalStr = Decimal("0.00")
    created_at: datetime


class PaymentDetailOut(PaymentOut):
    allocations: list[AllocationOut]
    tds: list[TdsOut]


class OpenDocumentOut(BaseModel):
    document_kind: str
    document_id: uuid.UUID
    number: str | None
    doc_date: date | None
    due_date: date | None
    currency: str
    document_total: DecimalStr
    open_amount: DecimalStr
    disputed_amount: DecimalStr
    payment_status: str
    taxable_excl_gst: DecimalStr
    expected_tds: DecimalStr | None = None
    tds_section_code: str | None = None
    tds_rate_pct: DecimalStr | None = None
    gst_tds_applicable: bool = False


class CreditBalanceOut(BaseModel):
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    currency: str
    balance: DecimalStr


# ---------------------------------------------------------------- TDS
class TdsSectionOut(_Out):
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    kind: str
    code: str
    label: str
    default_rate_pct: DecimalStr
    certificate_name: str
    active: bool


class TdsSectionIn(_In):
    kind: TdsKind
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    default_rate_pct: Annotated[Decimal, Field(ge=0, le=100, max_digits=7, decimal_places=4)]
    certificate_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)] = "Form 16A"
    active: bool = True


class CertificateIn(_In):
    certificate_status: Literal["expected", "received", "mismatch"]
    certificate_ref: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    certificate_received_at: date | None = None


class TdsReceivableRow(BaseModel):
    client_id: uuid.UUID
    quarter: str
    kind: str
    certificate_status: str
    count: int
    amount: DecimalStr
    currency: str


# ---------------------------------------------------------------- write-offs, disputes
class WriteOffIn(_In):
    document_kind: ReceivableDocumentKind
    document_id: uuid.UUID
    amount: PositiveMoney
    reason: Reason


class WriteOffRequestOut(_Out):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    client_id: uuid.UUID
    document_kind: str
    document_id: uuid.UUID
    currency: str
    requested_amount: DecimalStr
    reason: str
    status: str
    status_reason: str | None
    approval_request_id: uuid.UUID | None
    write_off_id: uuid.UUID | None
    created_at: datetime


class DisputeIn(_In):
    document_kind: ReceivableDocumentKind
    document_id: uuid.UUID
    amount: PositiveMoney
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    line_ids: Annotated[list[uuid.UUID], Field(max_length=200)] = Field(default_factory=list)
    owner_id: uuid.UUID | None = None


class DisputeResolveIn(_In):
    status: Literal["resolved_credit_note", "resolved_reissue", "rejected"]
    resolution_note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None
    linked_credit_note_id: uuid.UUID | None = None


class DisputeOut(_Out):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    client_id: uuid.UUID
    document_kind: str
    document_id: uuid.UUID
    amount: DecimalStr
    currency: str
    reason: str
    owner_id: uuid.UUID | None
    status: str
    resolution_note: str | None
    linked_credit_note_id: uuid.UUID | None
    opened_at: datetime
    resolved_at: datetime | None
    age_days: int = 0


# ---------------------------------------------------------------- aging, DSO
class AgingRow(BaseModel):
    key: str
    label: str
    client_id: uuid.UUID | None = None
    document_kind: str | None = None
    document_id: uuid.UUID | None = None
    due_date: date | None = None
    currency: str
    buckets: dict[str, DecimalStr]
    total_open: DecimalStr
    total_open_inr: DecimalStr
    disputed: DecimalStr
    unallocated_credit: DecimalStr


class AgingOut(BaseModel):
    as_of: date
    legal_entity_id: uuid.UUID
    group_by: str
    bucket_labels: list[str]
    rows: list[AgingRow]
    totals_inr: dict[str, DecimalStr]


class DsoOut(BaseModel):
    legal_entity_id: uuid.UUID
    period_start: date
    period_end: date
    dso: DecimalStr | None
    ar_open_inr: DecimalStr
    net_sales_inr: DecimalStr


# ---------------------------------------------------------------- collections
class ActivityIn(_In):
    client_id: uuid.UUID
    document_kind: ReceivableDocumentKind | None = None
    document_id: uuid.UUID | None = None
    kind: Literal["call", "email", "meeting", "note", "promise_to_pay"]
    body: Annotated[str, StringConstraints(max_length=5000)] | None = None
    ptp_amount: PositiveMoney | None = None
    ptp_currency: Annotated[str, StringConstraints(min_length=3, max_length=3, to_upper=True)] | None = None
    ptp_date: date | None = None
    owner_id: uuid.UUID | None = None


class ActivityOut(_Out):
    id: uuid.UUID
    client_id: uuid.UUID
    document_kind: str | None
    document_id: uuid.UUID | None
    kind: str
    body: str | None
    ptp_amount: DecimalStr | None
    ptp_currency: str | None
    ptp_date: date | None
    ptp_status: str | None
    owner_id: uuid.UUID | None
    task_id: uuid.UUID | None
    occurred_at: datetime
    created_by: uuid.UUID | None


class CollectionSettingsIn(_In):
    do_not_remind: bool | None = None
    owner_id: uuid.UUID | None = None


class CollectionSettingsOut(BaseModel):
    client_id: uuid.UUID
    do_not_remind: bool
    owner_id: uuid.UUID | None


class QueueRow(BaseModel):
    client_id: uuid.UUID
    client_name: str | None
    document_kind: str
    document_id: uuid.UUID
    number: str | None
    due_date: date | None
    days_overdue: int
    currency: str
    open_amount: DecimalStr
    disputed_amount: DecimalStr
    collectible: DecimalStr
    do_not_remind: bool
    open_ptp_date: date | None = None


class ReminderIn(_In):
    document_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=200)]
    template_id: uuid.UUID | None = None
    subject: Annotated[str, StringConstraints(max_length=300)] | None = None
    body: Annotated[str, StringConstraints(max_length=20000)] | None = None


class ReminderOut(BaseModel):
    sent: list[uuid.UUID]
    skipped: list[dict[str, Any]]


# ---------------------------------------------------------------- statements
class StatementLine(BaseModel):
    date: date
    kind: str
    reference: str | None
    description: str
    debit: DecimalStr
    credit: DecimalStr
    balance: DecimalStr


class StatementOut(BaseModel):
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID | None
    currency: str
    start: date
    end: date
    opening_balance: DecimalStr
    closing_balance: DecimalStr
    lines: list[StatementLine]


# ---------------------------------------------------------------- bank import
class BankRowOut(_Out):
    id: uuid.UUID
    row_no: int
    date: date | None
    amount: DecimalStr | None
    currency: str
    reference: str | None
    narrative: str | None
    match_kind: str
    matched_document_ids: list[uuid.UUID]
    client_id: uuid.UUID | None
    soft_duplicate_of: uuid.UUID | None
    payment_id: uuid.UUID | None
    error: str | None


class BankImportOut(BaseModel):
    batch_id: uuid.UUID
    status: str
    legal_entity_id: uuid.UUID
    rows: list[BankRowOut]


class BankApplyIn(_In):
    row_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=1000)]
    mode: PaymentMode = "neft"


# ---------------------------------------------------------------- accounting export
class ExportTemplateIn(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    columns: Annotated[list[str], Field(max_length=60)] = Field(default_factory=list)
    account_map: dict[str, str] = Field(default_factory=dict)


class ExportTemplateOut(_Out):
    id: uuid.UUID
    name: str
    columns: list[str]
    account_map: dict[str, str]
    active: bool


class ExportIn(_In):
    legal_entity_id: uuid.UUID
    from_date: date
    to_date: date
    template_id: uuid.UUID | None = None
    format: Literal["csv", "xlsx"] = "csv"


class ExportOut(_Out):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    from_date: date
    to_date: date
    template_id: uuid.UUID | None
    format: str
    version: int
    status: str
    row_count: int
    flagged_count: int
    error: str | None
    created_at: datetime


# ---------------------------------------------------------------- periods, reconciliation
class CheckResultOut(BaseModel):
    name: str
    label: str
    severity: Literal["blocker", "warning"]
    count: int
    link_path: str | None


class PeriodOut(BaseModel):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    year: int
    month: int
    status: str
    closed_at: datetime | None
    reopened_count: int
    pending_reopen: bool = False


class CloseIn(_In):
    acknowledged_warnings: Annotated[list[str], Field(max_length=50)] = Field(default_factory=list)


class ReopenIn(_In):
    reason: Reason


class CloseEventOut(_Out):
    id: uuid.UUID
    accounting_period_id: uuid.UUID
    legal_entity_id: uuid.UUID
    year: int
    month: int
    action: str
    actor_kind: str
    actor_id: uuid.UUID | None
    reason: str | None
    request_status: str | None
    approval_request_id: uuid.UUID | None
    at: datetime


class PriorPeriodItem(BaseModel):
    kind: str
    id: uuid.UUID
    date: date
    amount: DecimalStr
    currency: str


class PeriodReportOut(BaseModel):
    events: list[CloseEventOut]
    prior_period_items: list[PriorPeriodItem]


class ReconRunOut(_Out):
    id: uuid.UUID
    as_of: date
    started_at: datetime
    finished_at: datetime | None
    status: str
    findings_count: int
    error: str | None


class ReconFindingOut(_Out):
    id: uuid.UUID
    run_id: uuid.UUID
    check: str
    severity: str
    subject_kind: str
    subject_id: uuid.UUID | None
    details: dict[str, Any]
