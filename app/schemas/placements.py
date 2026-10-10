"""Placement contract DTOs (Epic 13 contract layer, AD-25 / AD-36; filled by Epic 19). Frozen; Decimals
serialise as strings. Changing a field needs a spine amendment (``tests/contracts/signatures.json``)."""

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.money import DecimalStr

WorkerType = Literal["employee", "contractor", "vendor_contractor"]
PayeeKind = Literal["worker", "vendor"]
RateUnit = Literal["hour", "day", "month", "fixed"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class PlacementSummary(_Frozen):
    id: uuid.UUID
    human_id: str
    status: str
    candidate_id: uuid.UUID
    client_id: uuid.UUID
    vendor_id: uuid.UUID | None
    legal_entity_id: uuid.UUID
    supplier_gst_registration_id: uuid.UUID | None
    payee_kind: PayeeKind
    worker_type: WorkerType
    planned_start_date: date
    actual_start_date: date | None
    end_date: date | None
    currency: str


class PlacementBillingContext(_Frozen):
    placement_id: uuid.UUID
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    supplier_gst_registration_id: uuid.UUID | None
    contract_id: uuid.UUID | None
    po_ids: list[uuid.UUID]
    currency: str
    rate_segment_id: uuid.UUID | None
    rate_unit: RateUnit | None
    bill_rate: DecimalStr | None
    work_state_code: str | None
    actual_start_date: date | None
    end_date: date | None


class PlacementPayContext(_Frozen):
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    legal_entity_id: uuid.UUID
    payee_kind: PayeeKind
    vendor_id: uuid.UUID | None
    worker_type: WorkerType
    currency: str
    rate_segment_id: uuid.UUID | None
    rate_unit: RateUnit | None
    pay_rate: DecimalStr | None
    vendor_rate: DecimalStr | None
    actual_start_date: date | None
    end_date: date | None
    burden_pct: DecimalStr | None = None
    vendor_burden_pct: DecimalStr | None = None


class RateSegmentOut(_Frozen):
    id: uuid.UUID
    effective_from: date
    effective_to: date | None
    rate_unit: RateUnit
    currency: str
    pay_rate: DecimalStr | None
    bill_rate: DecimalStr | None
    vendor_rate: DecimalStr | None
    burden_pct: DecimalStr | None = None
    vendor_burden_pct: DecimalStr | None = None


class PlacementCreateIn(_Frozen):
    candidate_id: uuid.UUID
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    vendor_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    demand_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    contract_id: uuid.UUID | None = None
    po_id: uuid.UUID | None = None
    worker_type: WorkerType
    engagement_kind: str | None = None
    planned_start_date: date
    planned_end_date: date | None = None
    actual_start_date: date | None = None
    currency: str
    work_state_code: str | None = None
    work_timezone: str = "Asia/Kolkata"
    rate_unit: RateUnit
    pay_rate: DecimalStr | None = None
    bill_rate: DecimalStr | None = None
    vendor_rate: DecimalStr | None = None
    burden_pct: DecimalStr | None = None
    vendor_burden_pct: DecimalStr | None = None
    account_manager_user_id: uuid.UUID | None = None
    recruiter_user_id: uuid.UUID | None = None
    imported: bool = False
    validate_only: bool = False


class WorkerPaymentIdentity(_Frozen):
    """Masked by default; ``pan`` / ``account_number`` only with ``unmask=True`` (``identity:view``, audited)."""

    candidate_id: uuid.UUID
    pan_last4: str | None
    pan_status: str | None
    bank_account_id: uuid.UUID | None
    bank_account_last4: str | None
    ifsc: str | None
    bank_active: bool
    pan: str | None = None
    account_number: str | None = None
