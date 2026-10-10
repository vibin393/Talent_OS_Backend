"""Billing contract DTOs (Epic 13 contract layer; filled by Epic 29). Frozen; Decimals as strings."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import DecimalStr

ReceivableDocumentKind = Literal["invoice", "debit_note"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class SettlementFacts(_Frozen):
    document_kind: ReceivableDocumentKind
    document_id: uuid.UUID
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    currency: str
    document_total: DecimalStr
    doc_date: date
    due_date: date | None
    status: str
    fx_rate: DecimalStr | None
    credit_noted_total: DecimalStr
    inr_document_total: DecimalStr | None
    # TDS base: taxable value excluding GST, pure-agent amounts and RCM tax
    taxable_excl_gst: DecimalStr


class ReceivableDocumentSummary(_Frozen):
    document_kind: ReceivableDocumentKind
    document_id: uuid.UUID
    number: str
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    currency: str
    document_total: DecimalStr
    doc_date: date
    due_date: date | None
    status: str
    fx_rate: DecimalStr | None
    inr_document_total: DecimalStr | None
    taxable_excl_gst: DecimalStr


# ================================================================ Epic 29 (Billing & GST invoicing)
DocKind = Literal["invoice", "credit_note", "debit_note"]
_Str = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
_Sac = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9]{4,8}$")]
_Ccy = Annotated[str, StringConstraints(to_upper=True, min_length=3, max_length=3)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BillingSettingsOut(_Out):
    round_off_mode: Literal["none", "nearest_rupee"]
    cn_gst_deadline: str
    allow_backdated_issue: bool
    stale_unbilled_days: int
    pending_irn_alert_hours: int
    annexure_enabled: bool


class BillingSettingsUpdate(_In):
    round_off_mode: Literal["none", "nearest_rupee"] | None = None
    cn_gst_deadline: Annotated[str, StringConstraints(pattern=r"^[0-1][0-9]-[0-3][0-9]$")] | None = None
    allow_backdated_issue: bool | None = None
    stale_unbilled_days: Annotated[int, Field(ge=1, le=365)] | None = None
    pending_irn_alert_hours: Annotated[int, Field(ge=1, le=720)] | None = None
    annexure_enabled: bool | None = None


class ClientBillingSettingsIn(_In):
    reverse_charge: bool = False
    annexure: bool = False


class ClientBillingSettingsOut(_Out):
    client_id: uuid.UUID
    reverse_charge: bool
    annexure: bool


class SacCodeIn(_In):
    code: _Sac
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    default_for_line_kind: list[str] = []
    active: bool = True


class SacCodeOut(_Out):
    id: uuid.UUID
    code: str
    description: str
    default_for_line_kind: list[str]
    active: bool


class TaxRateIn(_In):
    sac_code: _Sac
    rate_pct: Annotated[DecimalStr, Field(ge=0, le=100)]
    effective_from: date
    effective_to: date | None = None


class TaxRateOut(_Out):
    id: uuid.UUID
    sac_code: str
    rate_pct: DecimalStr
    effective_from: date
    effective_to: date | None


class BillingRuleIn(_In):
    basis: Literal["hourly", "daily", "monthly_fixed", "milestone", "fixed_fee"] = "hourly"
    prorate: bool = True
    proration_days: Literal["working", "calendar"] = "working"
    billable_categories: list[str] | None = None
    expense_rebill: Literal["at_cost", "markup", "pure_agent"] = "at_cost"
    markup_pct: DecimalStr | None = None
    grouping: Literal["per_placement", "per_po", "per_address_period"] = "per_placement"
    frequency: Literal["weekly", "monthly", "on_milestone"] = "monthly"
    line_template: Annotated[str, StringConstraints(max_length=300)] | None = None
    sac_code: _Sac = "998513"


class BillingRuleOut(_Out):
    id: uuid.UUID
    placement_id: uuid.UUID
    basis: str
    prorate: bool
    proration_days: str
    billable_categories: list[str] | None
    expense_rebill: str
    markup_pct: DecimalStr | None
    grouping: str
    frequency: str
    line_template: str | None
    sac_code: str
    version: int


class ManualItemIn(_In):
    kind: Literal["one_off", "placement_fee", "milestone"] = "one_off"
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    placement_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    item_date: date
    description: _Str
    sac_code: _Sac | None = None
    quantity: DecimalStr = Decimal(1)
    rate: DecimalStr
    currency: _Ccy = "INR"
    po_id: uuid.UUID | None = None


class ManualItemOut(_Out):
    id: uuid.UUID
    kind: str
    client_id: uuid.UUID
    placement_id: uuid.UUID | None
    item_date: date
    description: str
    quantity: DecimalStr
    rate: DecimalStr
    currency: str


class RunCreate(_In):
    legal_entity_id: uuid.UUID
    gst_registration_id: uuid.UUID | None = None
    period_start: date
    period_end: date
    client_id: uuid.UUID | None = None
    placement_ids: list[uuid.UUID] | None = None


class RunExceptionOut(_Out):
    id: uuid.UUID
    placement_id: uuid.UUID | None
    client_id: uuid.UUID | None
    kind: str
    severity: str
    details: dict[str, Any]


class RunOut(_Out):
    id: uuid.UUID
    human_id: str
    legal_entity_id: uuid.UUID
    gst_registration_id: uuid.UUID | None
    period_start: date
    period_end: date
    client_id: uuid.UUID | None
    placement_ids: list[uuid.UUID] | None
    status: str
    progress_pct: int
    draft_count: int
    totals: dict[str, Any]
    error: str | None
    created_at: datetime


class TaxLineOut(_Out):
    component: str
    rate_pct: DecimalStr
    amount: DecimalStr
    charged: bool
    inr_amount: DecimalStr


class DocLineOut(_Out):
    id: uuid.UUID
    line_no: int
    line_kind: str
    placement_id: uuid.UUID | None
    po_id: uuid.UUID | None
    description: str
    sac_code: str
    category: str | None
    quantity: DecimalStr
    unit: str
    rate: DecimalStr
    multiplier: DecimalStr
    effective_rate: DecimalStr
    gross_amount: DecimalStr
    discount_amount: DecimalStr
    taxable_value: DecimalStr
    outside_taxable_value: bool
    tax_rate_pct: DecimalStr
    inr_taxable_value: DecimalStr
    is_manual: bool
    period_start: date | None
    period_end: date | None
    original_invoice_line_id: uuid.UUID | None = None
    taxes: list[TaxLineOut] = []


class DocSummaryOut(_Out):
    id: uuid.UUID
    kind: DocKind = "invoice"
    status: str
    number: str | None
    fiscal_year: str | None
    doc_date: date | None
    due_date: date | None
    preview_date: date | None
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    supplier_gst_registration_id: uuid.UUID | None
    currency: str
    taxable_total: DecimalStr
    charged_tax_total: DecimalStr
    document_total: DecimalStr
    inr_document_total: DecimalStr
    supply_type: str | None
    einvoice_status: str
    delivery_status: str
    stale: bool
    approved: bool
    created_at: datetime
    original_invoice_id: uuid.UUID | None = None
    invoice_run_id: uuid.UUID | None = None
    client_name: str | None = None
    dsc_status: str = "not_required"  # Epic 51 (invoices / credit notes; debit notes stay not_required)


class DocDetailOut(DocSummaryOut):
    recipient_snapshot: dict[str, Any]
    supplier_snapshot: dict[str, Any]
    pos_state_code: str | None
    intra_state: bool
    reverse_charge: bool
    lut_ref: str | None
    fx_rate: DecimalStr | None
    fx_source: str | None
    fx_rate_date: date | None
    outside_taxable_total: DecimalStr
    rcm_tax_total: DecimalStr
    round_off: DecimalStr
    inr_taxable_total: DecimalStr
    inr_tax_total: DecimalStr
    warnings: list[str]
    notes: str | None
    content_hash: str | None
    issued_at: datetime | None
    cancel_basis: str | None
    cancelled_at: datetime | None
    irn: str | None
    ack_no: str | None
    ack_at: datetime | None
    signed_qr: str | None
    irn_report_by: date | None
    irn_cancelled_at: datetime | None
    irn_cancel_reason: str | None
    is_imported: bool
    reason: str | None = None
    rebill: bool | None = None
    gst_adjusted: bool | None = None
    amount_in_words: str = ""
    lines: list[DocLineOut] = []
    irn_cancel_deadline: datetime | None = None


class RunDetailOut(RunOut):
    exceptions: list[RunExceptionOut]
    drafts: list[DocSummaryOut]


class ManualLineIn(_In):
    description: _Str
    sac_code: _Sac | None = None
    quantity: DecimalStr = Decimal(1)
    unit: Annotated[str, StringConstraints(max_length=10)] = "item"
    rate: DecimalStr
    discount_amount: DecimalStr = Decimal(0)
    po_id: uuid.UUID | None = None
    pure_agent: bool = False


class LinePatch(_In):
    description: _Str | None = None
    sac_code: _Sac | None = None
    po_id: uuid.UUID | None = None
    discount_amount: DecimalStr | None = None
    quantity: DecimalStr | None = None
    rate: DecimalStr | None = None


class ManualInvoiceIn(_In):
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    gst_registration_id: uuid.UUID | None = None
    currency: _Ccy = "INR"
    contract_id: uuid.UUID | None = None
    notes: Annotated[str, StringConstraints(max_length=2000)] | None = None
    lines: list[ManualLineIn] = []


class FxOverrideIn(_In):
    fx_rate: DecimalStr
    source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=50)]


class IssueIn(_In):
    doc_date: date | None = None


class IssueBatchIn(_In):
    invoice_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=500)]
    doc_date: date | None = None


class IssueResult(BaseModel):
    id: uuid.UUID
    ok: bool
    number: str | None = None
    code: str | None = None
    message: str | None = None


class IssueBatchOut(BaseModel):
    issued: int
    failed: int
    results: list[IssueResult]


class SendIn(_In):
    to: Annotated[list[Annotated[str, StringConstraints(max_length=254)]], Field(max_length=20)] = []


class NoteLineIn(_In):
    original_invoice_line_id: uuid.UUID
    quantity: DecimalStr | None = None
    amount: DecimalStr | None = None


class CreditNoteIn(_In):
    reason: Literal["rate_correction", "hours_correction", "discount", "cancellation", "commercial", "other"]
    full: bool = False
    rebill: bool = False
    gst_adjusted: bool = True
    amount: DecimalStr | None = None  # gst_adjusted=false: the commercial credit amount
    lines: list[NoteLineIn] = []
    notes: Annotated[str, StringConstraints(max_length=2000)] | None = None


class DebitNoteIn(_In):
    reason: Literal["rate_correction", "hours_correction", "commercial", "other"] = "commercial"
    lines: Annotated[list[NoteLineIn], Field(min_length=1)]
    notes: Annotated[str, StringConstraints(max_length=2000)] | None = None


class CapComponent(BaseModel):
    component: str
    original: DecimalStr
    remaining: DecimalStr


class LineCap(BaseModel):
    line_id: uuid.UUID
    line_no: int
    description: str
    effective_rate: DecimalStr
    quantity: DecimalStr
    original_taxable: DecimalStr
    remaining_taxable: DecimalStr
    remaining_quantity: DecimalStr
    components: list[CapComponent]


class NoteCapsOut(BaseModel):
    invoice_id: uuid.UUID
    currency: str
    gst_deadline: date | None
    deadline_passed: bool
    imported: bool
    lines: list[LineCap]


class IrnIn(_In):
    irn: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[0-9a-f]{64}$")]
    ack_no: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]
    ack_at: datetime
    signed_qr: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] | None = None


class IrnCancelIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200)]


class EinvoiceImportRow(BaseModel):
    gstin: str | None
    doc_type: str | None
    number: str | None
    irn: str | None
    ack_no: str | None
    ack_at: str | None
    signed_qr: str | None = None
    status: Literal["matched", "unmatched", "already_registered", "conflict", "invalid"]
    document_kind: str | None = None
    document_id: uuid.UUID | None = None
    message: str | None = None


class EinvoiceImportOut(BaseModel):
    batch_id: uuid.UUID
    status: str
    duplicate_of: uuid.UUID | None = None
    counts: dict[str, int]
    rows: list[EinvoiceImportRow]


class EinvoiceApplyOut(BaseModel):
    batch_id: uuid.UUID
    applied: int


class IntegrityFindingOut(BaseModel):
    check: str
    severity: str
    subject_kind: str
    subject_id: uuid.UUID | None
    details: dict[str, Any]


class CreditNoteFacts(_Frozen):
    id: uuid.UUID
    invoice_id: uuid.UUID
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    currency: str
    document_total: DecimalStr
    doc_date: date | None
    status: str
    fx_rate: DecimalStr | None
    number: str | None


class OpeningInvoiceRow(BaseModel):
    """Epic 35 go-live: one legacy invoice (totals only; no lines, no postings)."""

    number: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16)]
    doc_date: date
    due_date: date | None = None
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    gst_registration_id: uuid.UUID | None = None
    currency: str = "INR"
    taxable_total: DecimalStr
    tax_total: DecimalStr = Decimal(0)
    document_total: DecimalStr
    fx_rate: DecimalStr | None = None
    sequence: int | None = None  # the counter value this number represents (raises the counter)


class InvoiceSummary(_Frozen):
    id: uuid.UUID
    number: str
    status: str
    document_total: DecimalStr
    currency: str
    validated_only: bool = False


# ---------------------------------------------------------------- Epic 63: side-effect-free invoice run preview
PreviewExceptionCategory = Literal[
    "po_missing", "po_cap_breach", "rate_mismatch", "unapproved_time", "billing_rule_missing", "mixed_currency", "other"
]


class RunPreviewException(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    category: PreviewExceptionCategory
    placement_id: uuid.UUID | None = None
    severity: str = "error"
    details: dict[str, Any] = Field(default_factory=dict)


class RunPreviewClient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    client_name: str | None = None
    currency: str | None = None
    drafts: int = 0
    items: int = 0
    total: DecimalStr = Decimal(0)
    totals: dict[str, str] = Field(default_factory=dict)
    exceptions: list[RunPreviewException] = Field(default_factory=list)


class RunPreviewOut(BaseModel):
    """What ``create_run`` would build now (Epic 63 Billing Agent): nothing is written, no number is assigned."""

    model_config = ConfigDict(extra="forbid")

    legal_entity_id: uuid.UUID
    period_start: date
    period_end: date
    client_id: uuid.UUID | None = None
    clients: list[RunPreviewClient] = Field(default_factory=list)
    draft_count: int = 0
    totals: dict[str, str] = Field(default_factory=dict)
    exception_count: int = 0
    digest: str = ""
