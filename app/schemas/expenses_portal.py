"""Expense portal DTOs (Epic 27, AD-26 / AD-27).

* Worker ("My Work"): the Worker's own costs -- no ``bill_amount``, markup or re-bill terms.
* Client: billable lines only, with the line amount in claim currency and ``bill_amount`` (what the Client
  pays); never non-billable lines, Worker reimbursement totals, markup % or pay data.

Both are registered with the field-visibility guard; Worker and Client schemas are separate classes.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.field_visibility import register_portal_schema
from app.core.money import DecimalStr
from app.schemas.expenses import LineDecisionIn


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ Worker
class WorkerClaimIn(_In):
    placement_id: uuid.UUID
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class WorkerClaimUpdate(_In):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


@register_portal_schema("worker")
class WorkerClaimablePlacement(BaseModel):
    placement_id: uuid.UUID
    client_name: str | None
    role_title: str | None
    currency: str
    start_date: date | None
    end_date: date | None


@register_portal_schema("worker")
class WorkerExpenseSettings(BaseModel):
    ai_receipt_read_enabled: bool
    max_receipt_mb: int
    allowed_receipt_types: list[str]


@register_portal_schema("worker")
class WorkerClaimSummary(BaseModel):
    id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    client_name: str | None
    role_title: str | None
    currency: str
    title: str
    status: str
    submitted_at: datetime | None
    total_claimed: DecimalStr
    total_approved: DecimalStr
    line_count: int


@register_portal_schema("worker")
class WorkerLine(BaseModel):
    id: uuid.UUID
    expense_date: date
    category: str
    description: str | None
    amount: DecimalStr
    currency: str
    km: DecimalStr | None
    fx_rate: DecimalStr | None
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


@register_portal_schema("worker")
class WorkerClaimOut(WorkerClaimSummary):
    editable: bool
    can_recall: bool
    rejection_comment: str | None
    placement_start: date | None
    placement_end: date | None
    lines: list[WorkerLine]


@register_portal_schema("worker")
class ReceiptOut(BaseModel):
    file_id: uuid.UUID
    original_name: str
    content_type: str
    size_bytes: int
    scan_status: str


@register_portal_schema("worker")
class ReceiptSuggestion(BaseModel):
    """AI suggestions only (AD-6) -- the Worker confirms every field; ``amount`` is a decimal string."""

    expense_date: date | None = None
    merchant: str | None = None
    amount: DecimalStr | None = None
    currency: str | None = None
    category: str | None = None


@register_portal_schema("worker")
class ReceiptReadOut(BaseModel):
    ok: bool
    suggestion: ReceiptSuggestion | None
    error_code: str | None = None


# ================================================================ Client
@register_portal_schema("client_user")
class ClientClaimSummary(BaseModel):
    id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    worker_name: str | None
    role_title: str | None
    currency: str
    title: str
    status: str
    submitted_at: datetime | None
    billable_line_count: int
    billable_total: DecimalStr
    approval_request_id: uuid.UUID | None
    awaiting_client: bool


@register_portal_schema("client_user")
class ClientClaimLine(BaseModel):
    id: uuid.UUID
    expense_date: date
    category: str
    description: str | None
    amount_claim_ccy: DecimalStr
    bill_amount: DecimalStr | None
    has_receipt: bool
    line_status: str
    decision_comment: str | None


@register_portal_schema("client_user")
class ClientClaimDetail(ClientClaimSummary):
    lines: list[ClientClaimLine]
    line_decisions_supported: bool = True


class ClientClaimDecideIn(_In):
    action: Literal["approve", "reject"]
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] | None = None
    line_decisions: list[LineDecisionIn] = Field(default_factory=list, max_length=500)
