"""Epic 31 internal API schemas (extensions, conversion, offboarding, early termination, exit settings).

Compensation fields (``pay_rate``, ``bill_rate``, ``vendor_rate``, ``burden_pct``, ``margin_*``,
``offered_compensation``) are masked by the router without ``compensation:view``.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import DecimalStr

ExtensionStatus = Literal[
    "proposed", "pending_approval", "pending_client_confirmation", "confirmed", "declined", "withdrawn"
]
CaseReason = Literal[
    "end_of_term", "client_termination", "worker_resignation", "converted", "misconduct", "other"
]
CaseStatus = Literal["initiated", "in_progress", "ready_to_close", "closed", "cancelled"]
ItemStatus = Literal["pending", "done", "waived", "not_applicable"]
OwnerRole = Literal["account_manager", "recruiter", "hr", "finance", "payroll"]
CheckStatus = Literal["pass", "warn", "fail"]
_Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
_Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- settings / templates
class ExitSettingsOut(_Out):
    alert_offsets_days: list[int]
    notify_client_users: bool
    bench_excluded_reasons: list[str]
    early_attrition_days: int
    auto_case_grace_days: int


class ExitSettingsUpdate(_In):
    alert_offsets_days: list[Annotated[int, Field(ge=1, le=365)]] | None = Field(default=None, max_length=6)
    notify_client_users: bool | None = None
    bench_excluded_reasons: list[CaseReason] | None = None
    early_attrition_days: Annotated[int, Field(ge=0, le=365)] | None = None
    auto_case_grace_days: Annotated[int, Field(ge=0, le=60)] | None = None


class TemplateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,59}$")]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    mandatory: bool = True
    owner_role: OwnerRole = "account_manager"
    due_offset_days: Annotated[int, Field(ge=-90, le=90)] = 0
    verifier: str | None = None


class TemplateOut(_Out):
    id: uuid.UUID
    name: str
    is_default: bool
    items: list[TemplateItem]
    updated_at: datetime


class TemplateIn(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    is_default: bool = False
    items: list[TemplateItem] = Field(min_length=1, max_length=40)


class TemplateUpdate(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    is_default: bool | None = None
    items: list[TemplateItem] | None = Field(default=None, min_length=1, max_length=40)


# ---------------------------------------------------------------- ending soon
class EndingSoonRow(_Out):
    placement_id: uuid.UUID
    human_id: str
    candidate_id: uuid.UUID
    candidate_name: str | None
    client_id: uuid.UUID
    client_name: str | None
    role_title: str | None
    worker_type: str | None
    end_date: date
    days_left: int
    open_extension_id: uuid.UUID | None
    open_case_id: uuid.UUID | None


# ---------------------------------------------------------------- extensions
class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    details: dict[str, Any] = Field(default_factory=dict)
    overridable: bool = False


class ExtensionIn(_In):
    new_end_date: date
    segment_effective_from: date | None = None
    rate_unit: Literal["hour", "day", "month", "fixed"] | None = None
    pay_rate: DecimalStr | None = Field(default=None, ge=0)
    bill_rate: DecimalStr | None = Field(default=None, ge=0)
    vendor_rate: DecimalStr | None = Field(default=None, ge=0)
    burden_pct: DecimalStr | None = Field(default=None, ge=0, le=500)
    po_id: uuid.UUID | None = None
    note: _Text | None = None


class ExtensionPreviewOut(BaseModel):
    checks: list[CheckResult]
    margin_amount: DecimalStr | None
    margin_pct: DecimalStr | None
    blocking: bool


class ExtensionOut(_Out):
    id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    current_end_date: date
    new_end_date: date
    segment_effective_from: date | None
    rate_unit: str | None
    pay_rate: DecimalStr | None
    bill_rate: DecimalStr | None
    vendor_rate: DecimalStr | None
    burden_pct: DecimalStr | None
    currency: str | None
    po_id: uuid.UUID | None
    status: ExtensionStatus
    checks: list[CheckResult]
    approval_request_id: uuid.UUID | None
    exception_approval_request_id: uuid.UUID | None
    exception_status: str
    client_response_by_kind: str | None
    client_response_at: datetime | None
    client_evidence_file_id: uuid.UUID | None
    client_comment: str | None
    confirmed_by: uuid.UUID | None
    confirmed_at: datetime | None
    rate_segment_id: uuid.UUID | None
    withdraw_reason: str | None
    extension_letter_document_id: uuid.UUID | None
    send_extension_letter: bool
    note: str | None
    created_at: datetime


class ExceptionRequestIn(_In):
    reason: _Reason


class WithdrawIn(_In):
    reason: _Reason


class ConfirmIn(_In):
    send_extension_letter: bool = False


class RecordedResponseIn(_In):
    decision: Literal["confirm", "decline"]
    evidence_file_id: uuid.UUID
    comment: _Text | None = None
    send_extension_letter: bool = False


class LinkOut(BaseModel):
    url: str
    expires_at: datetime


# ---------------------------------------------------------------- conversion
class ConversionIn(_In):
    conversion_date: date
    offered_compensation: DecimalStr = Field(ge=0)
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    note: _Text | None = None


class FeePreviewIn(_In):
    offered_compensation: DecimalStr = Field(ge=0)
    conversion_date: date | None = None


class FeePreviewOut(BaseModel):
    fee_rule: dict[str, Any]
    fee_proposal: DecimalStr | None
    currency: str


class ConversionOut(_Out):
    id: uuid.UUID
    placement_id: uuid.UUID
    conversion_date: date
    offered_compensation: DecimalStr | None
    currency: str
    fee_rule_snapshot: dict[str, Any]
    fee_proposal: DecimalStr | None
    offboarding_case_id: uuid.UUID | None
    created_at: datetime


# ---------------------------------------------------------------- offboarding
class OffboardingStartIn(_In):
    reason: CaseReason
    last_working_day: date
    notice_date: date | None = None
    reason_note: _Text | None = None
    template_id: uuid.UUID | None = None


class EarlyTerminationIn(_In):
    initiated_by: Literal["client", "worker"]
    notice_date: date
    requested_lwd: date
    reason: _Text | None = None
    template_id: uuid.UUID | None = None


class OffboardingItemOut(_Out):
    id: uuid.UUID
    case_id: uuid.UUID
    position: int
    kind: str
    label: str
    mandatory: bool
    owner_role: str | None
    owner_id: uuid.UUID | None
    owner_name: str | None = None
    due_date: date | None
    task_id: uuid.UUID | None
    verifier: str | None
    status: ItemStatus
    waived_reason: str | None
    completed_by_kind: str | None
    completed_at: datetime | None
    evidence_file_id: uuid.UUID | None
    document_id: uuid.UUID | None
    note: str | None
    auto_verified: bool
    verifier_details: dict[str, Any] | None


class OffboardingCaseOut(_Out):
    id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    placement_human_id: str | None = None
    candidate_name: str | None = None
    client_name: str | None = None
    worker_type: str | None = None
    reason: CaseReason
    reason_note: str | None
    notice_date: date | None
    requested_lwd: date | None
    last_working_day: date
    previous_end_date: date | None
    notice_period_days_snapshot: int | None
    notice_shortfall_days: int | None
    early_termination_by: str | None
    early_attrition: bool
    bench_eligible: bool | None
    status: CaseStatus
    initiated_by_kind: str
    client_confirmed_at: datetime | None
    closed_at: datetime | None
    cancel_reason: str | None
    created_at: datetime
    items_total: int = 0
    items_open: int = 0


class FeedbackIn(_In):
    answers: dict[str, Any] = Field(default_factory=dict)


class FeedbackOut(_Out):
    answers: dict[str, Any]
    recorded_by_kind: str
    created_at: datetime


class SettlementLine(BaseModel):
    kind: str
    label: str
    status: ItemStatus
    auto_verified: bool
    details: dict[str, Any] | None
    link: str | None


class OffboardingCaseDetail(OffboardingCaseOut):
    items: list[OffboardingItemOut] = Field(default_factory=list)
    feedback: FeedbackOut | None = None
    settlement: list[SettlementLine] = Field(default_factory=list)
    conversion: ConversionOut | None = None


class ItemCompleteIn(_In):
    evidence_file_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    note: _Text | None = None


class ItemWaiveIn(_In):
    reason: _Reason


class CancelIn(_In):
    reason: _Reason


class ExitFacts(BaseModel):
    placement_id: uuid.UUID
    case_id: uuid.UUID | None
    reason: str | None
    last_working_day: date | None
    early_attrition: bool
    notice_shortfall_days: int | None
    recruiter_user_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
