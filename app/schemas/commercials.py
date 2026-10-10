"""Client commercials contract DTOs (Epic 13 contract layer; filled by Epic 16). Frozen; Decimals as strings.

The first block (``RateCheckRequest`` .. ``ContractDraftIn``) is the frozen contract layer
(``tests/contracts/signatures.json``); the Epic 16 API schemas follow it.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import DecimalStr, MoneyOut

RateCheckSubjectKind = Literal[
    "demand", "vendor_release", "submission", "offer", "placement", "extension", "rate_segment"
]
RateCheckStatus = Literal["not_applicable", "within", "outside", "no_match"]
ContractType = Literal["msa", "sow", "amendment", "vendor_agreement"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class RateCheckRequest(_Frozen):
    subject_kind: RateCheckSubjectKind
    subject_id: uuid.UUID | None
    client_id: uuid.UUID
    contract_id: uuid.UUID | None = None
    on_date: date
    role: str | None = None
    location: str | None = None
    experience_years: DecimalStr | None = None
    bill_rate: DecimalStr | None = None
    vendor_rate: DecimalStr | None = None
    currency: str
    rate_unit: str


class RateCheckResult(_Frozen):
    status: RateCheckStatus
    matched_line_id: uuid.UUID | None
    min_bill_rate: DecimalStr | None
    max_bill_rate: DecimalStr | None
    max_vendor_rate: DecimalStr | None
    requires_exception: bool
    rate_check_id: uuid.UUID | None = None


class PoBalance(_Frozen):
    po_id: uuid.UUID
    currency: str
    value_cap: DecimalStr | None
    consumed_amount: DecimalStr
    remaining_amount: DecimalStr | None
    hours_cap: DecimalStr | None
    consumed_hours: DecimalStr
    remaining_hours: DecimalStr | None
    valid_from: date
    valid_to: date
    status: str


class ContractSummary(_Frozen):
    id: uuid.UUID
    human_id: str
    contract_type: ContractType
    client_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    legal_entity_id: uuid.UUID
    status: str
    start_date: date
    end_date: date | None


class ContractDraftIn(_Frozen):
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    contract_type: ContractType
    start_date: date
    end_date: date | None = None
    source_opportunity_id: uuid.UUID | None = None
    document_file_ids: list[uuid.UUID] = []
    title: str | None = None
    parent_contract_id: uuid.UUID | None = None


# ======================================================================================================
# Epic 16 API schemas (not part of the frozen contract layer above).
# ======================================================================================================
TaxTreatment = Literal["registered", "unregistered", "sez", "export"]
DeliveryPreference = Literal["email", "portal", "email_and_portal"]
PreferredVendorStatus = Literal["none", "empanelled", "preferred"]
ContractStatus = Literal["draft", "pending_approval", "active", "expired", "terminated"]
ConversionFeeKind = Literal["none", "pct_of_ctc", "fixed"]
# Epic 45 (FR-227): SOW pricing models (foundation column ``contracts.pricing_model``).
PricingModel = Literal["time_and_materials", "fixed_price", "milestone"]
ContractDocumentKind = Literal["draft", "signed", "annex", "other"]
MilestoneStatus = Literal["planned", "accepted", "cancelled"]
RateCardStatus = Literal["draft", "published", "superseded", "archived"]
RateUnit = Literal["hour", "day", "month", "year", "fixed"]
InternalRateResult = Literal[
    "ok", "out_of_range", "no_matching_line", "vendor_rate_exceeded", "not_validated"
]
ExceptionStatus = Literal["none", "pending", "approved", "rejected"]
PoStatus = Literal["active", "expired", "closed", "cancelled"]
PoValidityState = Literal["upcoming", "valid", "expired"]
PoValidityReason = Literal[
    "ok", "po_missing", "po_expired", "po_exhausted", "po_insufficient", "po_cancelled"
]
SourceDocKind = Literal["invoice", "credit_note", "debit_note"]

Str100 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Str200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Str300 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
OptStr200 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None
OptStr300 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None
OptStr20 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None
Currency3 = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=3, max_length=3)
]
Country2 = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=2, max_length=2)]
StateCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9]{2}$")]
GstinStr = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, max_length=15)]
Pan = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")
]
LeadDays = list[Annotated[int, Field(ge=1, le=365)]]
Thresholds = list[Annotated[int, Field(ge=1, le=100)]]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- payment terms / settings
class PaymentTermIn(_In):
    name: Str100
    days: int = Field(ge=0, le=365)
    is_default: bool = False


class PaymentTermUpdate(_In):
    name: Str100 | None = None
    days: int | None = Field(default=None, ge=0, le=365)
    is_default: bool | None = None


class PaymentTermOut(_Out):
    id: uuid.UUID
    name: str
    days: int
    is_default: bool
    archived_at: datetime | None


class ApprovalStepIn(BaseModel):
    name: str = "Approval"
    approver_kind: str
    approver_ref: dict[str, Any] = Field(default_factory=dict)


class CommercialsSettingsOut(_Out):
    contract_renewal_lead_days: list[int]
    po_alert_thresholds_pct: list[int]
    po_expiry_alert_days: int
    rate_exception_steps: list[dict[str, Any]]
    po_exception_steps: list[dict[str, Any]]


class CommercialsSettingsUpdate(_In):
    contract_renewal_lead_days: LeadDays | None = Field(default=None, max_length=6)
    po_alert_thresholds_pct: Thresholds | None = Field(default=None, max_length=6)
    po_expiry_alert_days: int | None = Field(default=None, ge=1, le=365)
    rate_exception_steps: list[ApprovalStepIn] | None = Field(default=None, min_length=1, max_length=10)
    po_exception_steps: list[ApprovalStepIn] | None = Field(default=None, min_length=1, max_length=10)


# ---------------------------------------------------------------- billing profile (FR-88)
class BillingProfileIn(_In):
    legal_name: Str300
    pan: Pan | None = None
    tax_treatment: TaxTreatment = "registered"
    payment_terms_id: uuid.UUID | None = None
    po_required: bool = False
    delivery_preference: DeliveryPreference = "email"
    tds_section_code: OptStr20 = None
    tds_rate_pct: DecimalStr | None = Field(default=None, ge=Decimal(0), le=Decimal(100))
    gst_tds_applicable: bool = False
    preferred_vendor_status: PreferredVendorStatus = "none"
    msp_name: OptStr200 = None
    vms_name: OptStr200 = None
    vms_reference: OptStr200 = None


class BillingAddressIn(_In):
    label: Str100
    line1: Str300
    line2: OptStr300 = None
    city: Str100
    state_code: StateCode | None = None
    postal_code: OptStr20 = None
    country: Country2 = "IN"
    gstin: GstinStr | None = None
    is_sez: bool = False
    is_default: bool = False


class BillingAddressUpdate(_In):
    label: Str100 | None = None
    line1: Str300 | None = None
    line2: OptStr300 = None
    city: Str100 | None = None
    state_code: StateCode | None = None
    postal_code: OptStr20 = None
    country: Country2 | None = None
    gstin: GstinStr | None = None
    is_sez: bool | None = None
    is_default: bool | None = None


class BillingAddressOut(_Out):
    id: uuid.UUID
    client_id: uuid.UUID
    billing_profile_id: uuid.UUID
    label: str
    line1: str
    line2: str | None
    city: str
    state_code: str | None
    postal_code: str | None
    country: str
    gstin: str | None
    is_sez: bool
    is_default: bool
    archived_at: datetime | None


class BillingContactsIn(_In):
    to: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    cc: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class BillingContactOut(BaseModel):
    contact_id: uuid.UUID
    role: Literal["to", "cc"]
    name: str
    email: str | None


class BillingProfileOut(_Out):
    id: uuid.UUID
    client_id: uuid.UUID
    legal_name: str
    pan: str | None
    tax_treatment: TaxTreatment
    payment_terms_id: uuid.UUID | None
    po_required: bool
    delivery_preference: DeliveryPreference
    tds_section_code: str | None
    tds_rate_pct: DecimalStr | None
    gst_tds_applicable: bool
    preferred_vendor_status: PreferredVendorStatus
    msp_name: str | None
    vms_name: str | None
    vms_reference: str | None
    addresses: list[BillingAddressOut] = Field(default_factory=list)
    contacts: list[BillingContactOut] = Field(default_factory=list)
    complete: bool = False


class BillingAddressFacts(_Frozen):
    id: uuid.UUID
    state_code: str | None
    gstin: str | None
    sez: bool
    country: str
    is_default: bool


class BillingContactsFacts(_Frozen):
    to: list[str]
    cc: list[str]


class BillingProfileFacts(_Frozen):
    """Read API for billing / receivables (they snapshot it at issue)."""

    client_id: uuid.UUID
    legal_name: str
    pan: str | None
    payment_terms_days: int | None
    addresses: list[BillingAddressFacts]
    tax_treatment: str
    po_required: bool
    billing_contacts: BillingContactsFacts
    tds_section_code: str | None
    tds_rate_pct: DecimalStr | None
    gst_tds_applicable: bool
    delivery_preference: str


# ---------------------------------------------------------------- contracts (FR-89)
class ContractCreate(_In):
    contract_type: ContractType
    client_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    legal_entity_id: uuid.UUID
    parent_contract_id: uuid.UUID | None = None
    title: Str300
    start_date: date
    end_date: date | None = None
    amends_end_date: date | None = None
    value_cap: DecimalStr | None = Field(default=None, gt=Decimal(0))
    currency: Currency3 = "INR"
    payment_terms_id: uuid.UUID | None = None
    conversion_fee_kind: ConversionFeeKind = "none"
    conversion_fee_value: DecimalStr | None = Field(default=None, ge=Decimal(0))
    conversion_fee_waive_after_months: int | None = Field(default=None, ge=0, le=120)
    placement_fee_pct: DecimalStr | None = Field(default=None, ge=Decimal(0), le=Decimal(100))
    reverse_charge: bool = False
    share_bgv_outcome: bool = False
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    renewal_lead_days: LeadDays | None = None
    owner_id: uuid.UUID | None = None
    pricing_model: PricingModel | None = None  # Epic 45: SOWs only


class ContractUpdate(_In):
    title: Str300 | None = None
    parent_contract_id: uuid.UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    amends_end_date: date | None = None
    value_cap: DecimalStr | None = Field(default=None, gt=Decimal(0))
    currency: Currency3 | None = None
    payment_terms_id: uuid.UUID | None = None
    conversion_fee_kind: ConversionFeeKind | None = None
    conversion_fee_value: DecimalStr | None = Field(default=None, ge=Decimal(0))
    conversion_fee_waive_after_months: int | None = Field(default=None, ge=0, le=120)
    placement_fee_pct: DecimalStr | None = Field(default=None, ge=Decimal(0), le=Decimal(100))
    reverse_charge: bool | None = None
    share_bgv_outcome: bool | None = None
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    renewal_lead_days: LeadDays | None = None
    owner_id: uuid.UUID | None = None
    pricing_model: PricingModel | None = None  # Epic 45: SOWs only


class ContractTerminateIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]
    termination_date: date


class ContractRef(_Out):
    id: uuid.UUID
    human_id: str
    contract_type: str
    title: str | None
    status: str
    start_date: date
    end_date: date | None


class ContractOut(_Out):
    id: uuid.UUID
    human_id: str
    contract_type: ContractType
    counterparty_kind: Literal["client", "vendor"]
    client_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    legal_entity_id: uuid.UUID
    parent_contract_id: uuid.UUID | None
    title: str | None
    status: ContractStatus
    start_date: date
    end_date: date | None
    amends_end_date: date | None
    value_cap: DecimalStr | None
    currency: str
    payment_terms_id: uuid.UUID | None
    conversion_fee_kind: ConversionFeeKind
    conversion_fee_value: DecimalStr | None
    conversion_fee_waive_after_months: int | None
    placement_fee_pct: DecimalStr | None
    reverse_charge: bool
    share_bgv_outcome: bool
    notice_period_days: int | None
    renewal_lead_days: list[int] | None
    owner_id: uuid.UUID | None
    approval_request_id: uuid.UUID | None
    approved_at: datetime | None
    signed_file_id: uuid.UUID | None
    signature_request_id: uuid.UUID | None
    activated_at: datetime | None
    terminated_at: datetime | None
    termination_date: date | None
    termination_reason: str | None
    opportunity_id: uuid.UUID | None
    # Epic 45 (FR-227): SOW pricing model (``value_cap`` is the SOW total value).
    pricing_model: PricingModel | None = None
    created_at: datetime
    updated_at: datetime
    approval_chain_configured: bool = False
    children: list[ContractRef] = Field(default_factory=list)


class ContractDocumentOut(_Out):
    id: uuid.UUID
    contract_id: uuid.UUID
    file_id: uuid.UUID
    kind: ContractDocumentKind
    uploaded_by: uuid.UUID | None
    created_at: datetime
    file_name: str | None = None


class ContractHistoryItem(BaseModel):
    id: uuid.UUID
    occurred_at: datetime
    body: str
    data: dict[str, Any] | None
    author_id: uuid.UUID | None


class ConversionFeeRule(_Frozen):
    kind: ConversionFeeKind
    value: DecimalStr | None
    waive_after_months: int | None


class ContractFacts(_Frozen):
    """Read API for placements / billing / extensions (``get_contract``)."""

    id: uuid.UUID
    human_id: str
    contract_type: str
    client_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    legal_entity_id: uuid.UUID
    status: str
    start_date: date
    end_date: date | None
    currency: str
    notice_period_days: int | None
    payment_terms_days: int | None
    placement_fee_pct: DecimalStr | None
    reverse_charge: bool
    share_bgv_outcome: bool
    conversion_fee_rule: ConversionFeeRule


class MilestoneIn(_In):
    name: Str200
    amount: DecimalStr = Field(gt=Decimal(0))
    due_date: date | None = None


class MilestoneUpdate(_In):
    name: Str200 | None = None
    amount: DecimalStr | None = Field(default=None, gt=Decimal(0))
    due_date: date | None = None


class MilestoneOut(BaseModel):
    id: uuid.UUID
    contract_id: uuid.UUID
    name: str
    amount: MoneyOut
    due_date: date | None
    status: MilestoneStatus
    accepted_at: datetime | None
    accepted_by: uuid.UUID | None
    billed: bool = False


# ---------------------------------------------------------------- rate cards (FR-90)
class RateCardIn(_In):
    client_id: uuid.UUID
    contract_id: uuid.UUID | None = None
    name: Str200
    effective_from: date


class RateCardTemplateIn(_In):
    name: Str200


class RateCardUpdate(_In):
    name: Str200 | None = None
    effective_from: date | None = None
    contract_id: uuid.UUID | None = None


class RateCardLineIn(_In):
    role: Str200
    location: OptStr200 = None
    experience_min_years: DecimalStr | None = Field(default=None, ge=Decimal(0), le=Decimal(999))
    experience_max_years: DecimalStr | None = Field(default=None, ge=Decimal(0), le=Decimal(999))
    min_bill_rate: DecimalStr = Field(ge=Decimal(0))
    max_bill_rate: DecimalStr = Field(ge=Decimal(0))
    currency: Currency3 = "INR"
    rate_unit: RateUnit = "hour"
    max_vendor_rate: DecimalStr | None = Field(default=None, ge=Decimal(0))
    markup_pct: DecimalStr | None = Field(default=None, ge=Decimal(0), le=Decimal(1000))


class RateCardLinesIn(_In):
    lines: list[RateCardLineIn] = Field(max_length=2000)


class RateCardLineOut(_Out):
    id: uuid.UUID
    role: str
    location: str | None
    experience_min_years: DecimalStr | None
    experience_max_years: DecimalStr | None
    min_bill_rate: DecimalStr
    max_bill_rate: DecimalStr
    currency: str
    rate_unit: str
    max_vendor_rate: DecimalStr | None
    markup_pct: DecimalStr | None


class RateCardOut(_Out):
    id: uuid.UUID
    human_id: str
    client_id: uuid.UUID | None
    contract_id: uuid.UUID | None
    is_template: bool
    name: str
    version: int
    status: RateCardStatus
    effective_from: date | None
    effective_to: date | None
    source_template_id: uuid.UUID | None
    published_at: datetime | None
    published_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    line_count: int = 0
    lines: list[RateCardLineOut] | None = None


class CopyTemplateIn(_In):
    client_id: uuid.UUID
    contract_id: uuid.UUID | None = None
    effective_from: date


class ImportRowError(BaseModel):
    row: int
    field: str | None
    code: str
    message: str


class RateCardImportOut(BaseModel):
    applied: int
    errors: list[ImportRowError]
    duplicate: bool = False


# ---------------------------------------------------------------- rate checks (FR-91)
class RateCheckOut(_Out):
    id: uuid.UUID
    client_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    input: dict[str, Any]
    result: InternalRateResult
    rate_card_id: uuid.UUID | None
    rate_card_line_id: uuid.UUID | None
    min_bill_rate: DecimalStr | None
    max_bill_rate: DecimalStr | None
    max_vendor_rate: DecimalStr | None
    approval_request_id: uuid.UUID | None
    exception_status: ExceptionStatus
    checked_at: datetime
    actor_kind: str
    actor_id: uuid.UUID | None


class RateCheckPreviewOut(BaseModel):
    result: InternalRateResult
    status: RateCheckStatus
    requires_exception: bool
    rate_card_id: uuid.UUID | None
    matched_line_id: uuid.UUID | None
    min_bill_rate: DecimalStr | None
    max_bill_rate: DecimalStr | None
    max_vendor_rate: DecimalStr | None


# ---------------------------------------------------------------- purchase orders (FR-92)
PoNumber = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Notes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]


class PurchaseOrderIn(_In):
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    contract_id: uuid.UUID | None = None
    po_number: PoNumber
    currency: Currency3 = "INR"
    value_cap: DecimalStr | None = Field(default=None, gt=Decimal(0))
    hours_cap: DecimalStr | None = Field(default=None, gt=Decimal(0))
    valid_from: date
    valid_to: date
    owner_id: uuid.UUID | None = None
    alert_thresholds_pct: Thresholds | None = Field(default=None, max_length=6)
    notes: Notes | None = None


class PurchaseOrderUpdate(_In):
    contract_id: uuid.UUID | None = None
    value_cap: DecimalStr | None = Field(default=None, gt=Decimal(0))
    hours_cap: DecimalStr | None = Field(default=None, gt=Decimal(0))
    valid_from: date | None = None
    valid_to: date | None = None
    owner_id: uuid.UUID | None = None
    alert_thresholds_pct: Thresholds | None = Field(default=None, max_length=6)
    notes: Notes | None = None


class PurchaseOrderOut(_Out):
    id: uuid.UUID
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    contract_id: uuid.UUID | None
    po_number: str
    currency: str
    value_cap: DecimalStr | None
    hours_cap: DecimalStr | None
    valid_from: date
    valid_to: date
    status: PoStatus
    owner_id: uuid.UUID | None
    file_id: uuid.UUID | None
    alert_thresholds_pct: list[int] | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    consumed_amount: DecimalStr = Decimal("0")
    consumed_hours: DecimalStr = Decimal("0")
    remaining_amount: DecimalStr | None = None
    remaining_hours: DecimalStr | None = None
    pct_used: DecimalStr | None = None
    validity: PoValidityState = "valid"


class PoConsumptionOut(_Out):
    id: uuid.UUID
    po_id: uuid.UUID
    source_doc_kind: SourceDocKind
    source_doc_id: uuid.UUID
    amount: DecimalStr
    hours: DecimalStr
    currency: str
    exception_approval_id: uuid.UUID | None
    recorded_at: datetime


class PoValidity(_Frozen):
    valid: bool
    reason: PoValidityReason
    remaining_amount: DecimalStr | None
    remaining_hours: DecimalStr | None


class PoRef(_Frozen):
    po_id: uuid.UUID
    po_number: str
    currency: str
    valid_from: date
    valid_to: date
    remaining_amount: DecimalStr | None
    remaining_hours: DecimalStr | None


class PoExceptionIn(_In):
    source_doc_kind: SourceDocKind
    source_doc_id: uuid.UUID
    amount: DecimalStr
    hours: DecimalStr | None = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]


class PoPlacementOut(BaseModel):
    placement_id: uuid.UUID
    human_id: str | None
    status: str | None
