"""Legal Entities, GST registrations, bank accounts, numbering series, periods and FX rates (Epic 13)."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import DecimalStr

EntityStatus = Literal["active", "inactive"]
DocType = Literal["invoice", "credit_note", "debit_note"]
FyFormat = Literal["YY-YY", "YYYY-YY", "YY", "none"]
Separator = Literal["/", "-", ""]
PeriodStatus = Literal["open", "closed"]

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Currency = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=3, max_length=3)]
StateCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9]{2}$")]
Pan = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")
]
Gstin = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=15, max_length=15)]
FiscalYear = Annotated[str, StringConstraints(pattern=r"^[0-9]{2}-[0-9]{2}$")]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- legal entities
class LegalEntityIn(_In):
    name: Name
    legal_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    pan: Pan | None = None
    cin: Annotated[str, StringConstraints(strip_whitespace=True, max_length=21)] | None = None
    address: dict[str, Any] = Field(default_factory=dict)
    state_code: StateCode | None = None
    country: Annotated[
        str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=2, max_length=2)
    ] = "IN"
    default_currency: Currency = "INR"
    timezone: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] = (
        "Asia/Kolkata"
    )
    logo_file_id: uuid.UUID | None = None
    signatory_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    signatory_title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class LegalEntityUpdate(_In):
    name: Name | None = None
    legal_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)] | None
    ) = None
    pan: Pan | None = None
    cin: Annotated[str, StringConstraints(strip_whitespace=True, max_length=21)] | None = None
    address: dict[str, Any] | None = None
    state_code: StateCode | None = None
    country: (
        Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=2, max_length=2)]
        | None
    ) = None
    default_currency: Currency | None = None
    timezone: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None = (
        None
    )
    logo_file_id: uuid.UUID | None = None
    signatory_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    signatory_title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class LegalEntityOut(_Out):
    id: uuid.UUID
    name: str
    legal_name: str
    pan: str | None
    cin: str | None
    address: dict[str, Any]
    state_code: str | None
    country: str
    default_currency: str
    timezone: str
    logo_file_id: uuid.UUID | None
    signatory_name: str | None
    signatory_title: str | None
    status: EntityStatus
    is_backfilled: bool
    created_at: datetime
    updated_at: datetime
    # Epic 14 (FR-76).
    deactivation_reason: str | None = None
    deactivated_at: datetime | None = None


# ---------------------------------------------------------------- GST registrations / LUTs
class GstRegistrationIn(_In):
    gstin: Gstin
    state_code: StateCode
    address: dict[str, Any] = Field(default_factory=dict)
    is_principal: bool = False
    einvoice_applicable: bool = False
    irp_reporting_limit: bool = False


class GstRegistrationUpdate(_In):
    address: dict[str, Any] | None = None
    is_principal: bool | None = None
    einvoice_applicable: bool | None = None
    irp_reporting_limit: bool | None = None
    status: EntityStatus | None = None


class GstRegistrationOut(_Out):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    gstin: str
    state_code: str
    address: dict[str, Any]
    is_principal: bool
    einvoice_applicable: bool
    irp_reporting_limit: bool
    status: EntityStatus
    created_at: datetime


class LutIn(_In):
    fiscal_year: FiscalYear
    lut_number: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    valid_from: date
    valid_to: date


class LutOut(_Out):
    id: uuid.UUID
    gst_registration_id: uuid.UUID
    fiscal_year: str
    lut_number: str
    valid_from: date
    valid_to: date


# ---------------------------------------------------------------- bank accounts (masked)
class BankAccountIn(_In):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    bank_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    account_number: Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=34)]
    ifsc: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")
        ]
        | None
    ) = None
    swift: (
        Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=8, max_length=11)]
        | None
    ) = None
    currency: Currency | None = None
    is_default: bool = False
    # Epic 14.
    branch: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    account_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class BankAccountOut(BaseModel):
    """The account number is never returned: only its last four characters."""

    id: uuid.UUID
    legal_entity_id: uuid.UUID
    label: str
    bank_name: str
    account_number_last4: str
    ifsc: str | None
    swift: str | None
    currency: str | None
    is_default: bool
    created_at: datetime
    # Epic 14.
    branch: str | None = None
    account_name: str | None = None
    archived_at: datetime | None = None


class DocumentBankAccount(BaseModel):
    """Decrypted company account for printing on documents (system read; never returned by an API)."""

    model_config = ConfigDict(frozen=True)

    bank_account_id: uuid.UUID
    legal_entity_id: uuid.UUID
    bank_name: str
    account_number: str
    ifsc: str | None
    swift: str | None
    currency: str | None


# ---------------------------------------------------------------- document series
class DocumentSeriesIn(_In):
    gst_registration_id: uuid.UUID | None = None
    doc_type: DocType
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    prefix: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)] = ""
    fy_format: FyFormat = "YY-YY"
    separator: Separator = "/"
    pad: Annotated[int, Field(ge=1, le=10)] = 4
    is_legacy: bool = False
    allow_backdated: bool = False


class DocumentSeriesUpdate(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    prefix: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)] | None = None
    fy_format: FyFormat | None = None
    separator: Separator | None = None
    pad: Annotated[int, Field(ge=1, le=10)] | None = None
    allow_backdated: bool | None = None
    status: EntityStatus | None = None


class DocumentSeriesOut(_Out):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    gst_registration_id: uuid.UUID | None
    doc_type: DocType
    name: str
    prefix: str
    fy_format: FyFormat
    separator: str
    pad: int
    is_legacy: bool
    allow_backdated: bool
    status: EntityStatus
    next_number_preview: str | None = None


# ---------------------------------------------------------------- periods
class AccountingPeriodOut(_Out):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    year: int
    month: int
    status: PeriodStatus
    closed_at: datetime | None
    closed_by: uuid.UUID | None
    reopened_count: int


class CloseCheckResultOut(BaseModel):
    name: str
    label: str
    blocking: bool
    count: int
    link_path: str | None


# ---------------------------------------------------------------- FX
class FxRateIn(_In):
    from_currency: Currency
    to_currency: Currency
    rate_date: date
    rate: Annotated[DecimalStr, Field(gt=0)]  # quantized to 6 dp by the service
    source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class FxRateOut(_Out):
    id: uuid.UUID
    from_currency: str
    to_currency: str
    rate_date: date
    rate: DecimalStr
    source: str
    created_at: datetime
    origin: str = "manual"  # Epic 51: manual | feed


# ---------------------------------------------------------------- Epic 14: admin deltas (FR-76)
class LegalEntityDeactivateIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = (
        None
    )


class LegalEntityReadinessOut(BaseModel):
    legal_entity_id: uuid.UUID
    ready_for_invoicing: bool
    # pan | state_code | principal_gst_registration | bank_account | signatory
    missing: list[str]


class LutPutIn(_In):
    lut_number: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    valid_from: date
    valid_to: date


class BankAccountUpdate(_In):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = (
        None
    )
    bank_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None
    ) = None
    account_number: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=34)] | None
    ) = None
    branch: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    account_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    ifsc: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")
        ]
        | None
    ) = None
    swift: (
        Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=8, max_length=11)]
        | None
    ) = None
    currency: Currency | None = None
    is_default: bool | None = None
    archived: bool | None = None


class BankAccountUnmaskedOut(BaseModel):
    """Returned only to holders of ``identity:view``; the read is audited."""

    id: uuid.UUID
    account_number: str


class GstStateOut(_Out):
    code: str
    name: str
    kind: str


class CurrencyOut(_Out):
    code: str
    name: str
    document_allowed: bool
