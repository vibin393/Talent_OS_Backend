"""Expenses DTOs, internal app (Epic 27, FR-137..FR-139). Decimals are strings on the wire (AD-12); request
amounts are strings too (a JSON float is refused with 422). Worker / Client portal schemas live in
``schemas/expenses_portal.py``.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import DecimalStr

ExpenseCategory = Literal["travel", "accommodation", "meals", "mileage", "equipment", "other"]
RebillMethod = Literal["at_cost", "markup", "not_billable"]
RebillTaxMode = Literal["taxable", "pure_agent"]
ClaimStatus = Literal[
    "draft", "submitted", "in_approval", "approved", "partially_approved", "rejected", "reimbursed", "stated"
]
LineFlag = Literal[
    "over_daily_limit",
    "over_claim_limit",
    "receipt_missing",
    "possible_duplicate_receipt",
    "category_disabled",
]

# Decimal request strings: optional sign (negative -> 422 ``negative_amount`` from the service), at most 3 dp
# (the service checks the currency's minor units).
AmountStr = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^-?\d{1,13}(\.\d{1,3})?$")]
KmStr = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^-?\d{1,7}(\.\d{1,2})?$")]
RateStr = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{1,9}(\.\d{1,4})?$")]
PctStr = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{1,3}(\.\d{1,4})?$")]
CurrencyStr = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{3}$")
]
Text4k = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ settings
class ExpenseSettingsOut(BaseModel):
    ai_receipt_read_enabled: bool
    duplicate_receipt_window_days: int
    max_receipt_mb: int
    allowed_receipt_types: list[str]


class ExpenseSettingsUpdate(_In):
    ai_receipt_read_enabled: bool | None = None
    duplicate_receipt_window_days: int | None = Field(default=None, ge=0, le=3650)
    max_receipt_mb: int | None = Field(default=None, ge=1, le=50)


# ================================================================ policies
class PolicyCategoryIn(_In):
    category: ExpenseCategory
    enabled: bool = True
    daily_limit: AmountStr | None = None
    claim_limit: AmountStr | None = None
    limit_currency: CurrencyStr = "INR"
    receipt_required_above: AmountStr | None = None
    mileage_rate_per_km: RateStr | None = None
    default_billable: bool = False
    rebill_method: RebillMethod = "at_cost"
    markup_pct: PctStr | None = None
    rebill_tax_mode: RebillTaxMode = "taxable"


class PolicyCategoryOut(BaseModel):
    category: str
    enabled: bool
    daily_limit: DecimalStr | None
    claim_limit: DecimalStr | None
    limit_currency: str
    receipt_required_above: DecimalStr | None
    mileage_rate_per_km: DecimalStr | None
    default_billable: bool
    rebill_method: str
    markup_pct: DecimalStr | None
    rebill_tax_mode: str


class PolicyIn(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    scope: Literal["tenant", "client"] = "tenant"
    client_id: uuid.UUID | None = None
    effective_from: date | None = None
    categories: list[PolicyCategoryIn] = Field(default_factory=list, max_length=6)


class PolicyVersionIn(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    effective_from: date | None = None
    categories: list[PolicyCategoryIn] = Field(default_factory=list, max_length=6)


class PolicyVersionRef(BaseModel):
    id: uuid.UUID
    version: int
    effective_from: date
    superseded_at: datetime | None


class PolicyOut(BaseModel):
    id: uuid.UUID
    name: str
    scope: str
    client_id: uuid.UUID | None
    client_name: str | None
    version: int
    effective_from: date
    superseded_at: datetime | None
    created_at: datetime
    categories: list[PolicyCategoryOut]
    versions: list[PolicyVersionRef] = Field(default_factory=list)


# ================================================================ claims (internal)
class LineIn(_In):
    """One expense line (Worker or proxy). ``amount`` is ignored for mileage (``km`` x policy rate)."""

    expense_date: date
    category: ExpenseCategory
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    amount: AmountStr | None = None
    currency: CurrencyStr | None = None
    km: KmStr | None = None
    receipt_file_id: uuid.UUID | None = None
    billable: bool | None = None
    reimbursable: bool = True
    justification: Text4k | None = None


class LinePreviewOut(BaseModel):
    amount: DecimalStr
    currency: str
    km: DecimalStr | None
    fx_rate: DecimalStr | None
    fx_rate_date: date | None
    amount_claim_ccy: DecimalStr
    claim_currency: str
    billable: bool
    flags: list[str]
    receipt_required: bool
    justification_required: bool


class ProxyClaimIn(_In):
    placement_id: uuid.UUID
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    proxy_reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ProxySubmitIn(_In):
    proxy_reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ReasonIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class LineDecisionIn(_In):
    line_id: uuid.UUID
    decision: Literal["approve", "reject"]
    comment: Text4k | None = None


class ClaimDecideIn(_In):
    action: Literal["approve", "reject"]
    comment: Text4k | None = None
    line_decisions: list[LineDecisionIn] = Field(default_factory=list, max_length=500)


class ApprovalStepOut(BaseModel):
    name: str
    outcome: str
    actor_kind: str | None
    comment: str | None
    decided_at: datetime | None


class LineDecisionOut(BaseModel):
    step: str
    decision: str
    comment: str | None
    actor_kind: str
    decided_at: datetime


class ClaimLineOut(BaseModel):
    id: uuid.UUID
    expense_date: date
    category: str
    description: str | None
    amount: DecimalStr
    currency: str
    km: DecimalStr | None
    fx_rate: DecimalStr | None
    fx_source: str | None
    fx_rate_date: date | None
    amount_claim_ccy: DecimalStr
    receipt_file_id: uuid.UUID | None
    receipt_name: str | None
    billable: bool
    reimbursable: bool
    flags: list[str]
    justification: str | None
    line_status: str
    decision_comment: str | None
    rebill_method: str | None
    markup_pct: DecimalStr | None  # masked without compensation:view
    rebill_tax_mode: str | None
    bill_amount: DecimalStr | None  # masked without compensation:view
    payable_posted: bool
    billing_posted: bool
    decisions: list[LineDecisionOut] = Field(default_factory=list)


class ClaimSummaryOut(BaseModel):
    id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    worker_name: str | None
    client_id: uuid.UUID
    client_name: str | None
    legal_entity_id: uuid.UUID
    currency: str
    title: str
    status: str
    submitted_at: datetime | None
    total_claimed: DecimalStr
    total_approved: DecimalStr
    line_count: int
    flags: list[str]
    is_proxy: bool
    unposted_lines: int


class ClaimDetailOut(ClaimSummaryOut):
    proxy_reason: str | None
    rejection_comment: str | None
    approval_request_id: uuid.UUID | None
    current_step: str | None
    approved_at: datetime | None
    reimbursed_at: datetime | None
    payroll_reference: str | None
    stated_at: datetime | None
    reopen_reason: str | None
    locked: bool
    can_reopen: bool
    lines: list[ClaimLineOut]
    approval_history: list[ApprovalStepOut]
