"""Epic 19 placement API schemas (internal routes). The frozen contract DTOs live in ``app.schemas.placements``."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import DecimalStr
from app.schemas.placements import RateUnit, WorkerType

EngagementKind = Literal["contract", "c2h", "permanent"]
DropReason = Literal["declined", "no_show", "bgv_failed", "client_cancelled"]
_Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PlacementSettingsOut(_Out):
    human_id_prefix: str
    touchpoint_offsets: list[int]
    gate_alert_offset_days: int
    ending_soon_days: int
    worker_type_by_employment_type: dict[str, Any]
    dropout_rules: dict[str, Any]
    touchpoint_email_template: str | None
    min_margin_pct: DecimalStr | None


class PlacementSettingsUpdate(_In):
    human_id_prefix: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2,6}$")] | None = None
    touchpoint_offsets: list[Annotated[int, Field(ge=0, le=90)]] | None = Field(default=None, max_length=10)
    gate_alert_offset_days: Annotated[int, Field(ge=0, le=60)] | None = None
    ending_soon_days: Annotated[int, Field(ge=1, le=365)] | None = None
    worker_type_by_employment_type: dict[str, WorkerType | None] | None = None
    dropout_rules: dict[str, Any] | None = None
    touchpoint_email_template: Annotated[str, StringConstraints(max_length=20000)] | None = None
    min_margin_pct: DecimalStr | None = None


class PlacementListItem(_Out):
    id: uuid.UUID
    human_id: str
    status: str
    candidate_id: uuid.UUID
    candidate_name: str | None = None
    client_id: uuid.UUID
    client_name: str | None = None
    vendor_id: uuid.UUID | None
    legal_entity_id: uuid.UUID
    worker_type: str | None
    engagement_kind: str | None
    role_title: str | None
    planned_start_date: date
    planned_end_date: date | None
    actual_start_date: date | None
    end_date: date | None
    dropout_risk: bool
    non_compliant: bool
    compliance_blocked: bool
    setup_items: list[str]
    ending_soon: bool = False


class PlacementOut(PlacementListItem):
    worker_human_id: str | None = None
    legal_entity_name: str | None = None
    application_id: uuid.UUID | None
    offer_id: uuid.UUID | None
    demand_id: uuid.UUID | None
    contract_id: uuid.UUID | None
    po_id: uuid.UUID | None
    rate_card_line_id: uuid.UUID | None
    supplier_gst_registration_id: uuid.UUID | None
    payee_kind: str
    work_location_text: str | None
    work_state_code: str | None
    work_timezone: str
    holiday_calendar_id: uuid.UUID | None
    approval_route_ref: str | None
    currency: str
    account_manager_user_id: uuid.UUID | None
    recruiter_user_id: uuid.UUID | None
    manager_user_id: uuid.UUID | None
    client_manager_contact_id: uuid.UUID | None
    joining_confirmed_at: datetime | None
    joining_confirmed_via: str | None
    drop_reason: str | None
    drop_note: str | None
    dropout_risk_rules: list[str]
    dropout_cleared_at: datetime | None
    counter_offer_at: datetime | None
    counter_offer_note: str | None
    onboarding_pct: int | None
    bgv_summary: dict[str, Any] | None
    lock_version: int
    created_at: datetime
    updated_at: datetime


class PlacementUpdate(_In):
    lock_version: int
    account_manager_user_id: uuid.UUID | None = None
    recruiter_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    client_manager_contact_id: uuid.UUID | None = None
    work_location_text: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    work_state_code: Annotated[str, StringConstraints(pattern=r"^[0-9A-Z]{2}$")] | None = None
    work_timezone: Annotated[str, StringConstraints(max_length=64)] | None = None
    holiday_calendar_id: uuid.UUID | None = None
    approval_route_ref: Annotated[str, StringConstraints(max_length=100)] | None = None
    role_title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None


class PlacementCreateBody(_In):
    """``POST /placements`` body: ``PlacementCreateIn`` plus presentation fields; the Legal Entity and worker type
    default like offer-created placements."""

    candidate_id: uuid.UUID
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    demand_id: uuid.UUID | None = None
    contract_id: uuid.UUID | None = None
    po_id: uuid.UUID | None = None
    worker_type: WorkerType | None = None
    engagement_kind: EngagementKind = "contract"
    planned_start_date: date
    planned_end_date: date | None = None
    actual_start_date: date | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "INR"
    work_state_code: str | None = None
    work_timezone: str = "Asia/Kolkata"
    work_location_text: Annotated[str, StringConstraints(max_length=300)] | None = None
    role_title: Annotated[str, StringConstraints(max_length=300)] | None = None
    rate_unit: RateUnit = "hour"
    pay_rate: DecimalStr | None = None
    bill_rate: DecimalStr | None = None
    vendor_rate: DecimalStr | None = None
    burden_pct: DecimalStr | None = None
    vendor_burden_pct: DecimalStr | None = None
    account_manager_user_id: uuid.UUID | None = None
    recruiter_user_id: uuid.UUID | None = None
    imported: bool = False
    validate_only: bool = False


class RateSegmentDetailOut(_Out):
    id: uuid.UUID
    placement_id: uuid.UUID
    effective_from: date
    effective_to: date | None
    rate_unit: str
    currency: str
    pay_rate: DecimalStr | None
    bill_rate: DecimalStr | None
    vendor_rate: DecimalStr | None
    burden_pct: DecimalStr | None
    vendor_burden_pct: DecimalStr | None
    margin_amount: DecimalStr | None
    margin_pct: DecimalStr | None
    rate_check: dict[str, Any] | None
    status: str
    approval_request_id: uuid.UUID | None
    reason: str | None
    created_by: uuid.UUID | None
    created_at: datetime


class RateSegmentIn(_In):
    effective_from: date
    pay_rate: DecimalStr | None = None
    bill_rate: DecimalStr | None = None
    vendor_rate: DecimalStr | None = None
    burden_pct: DecimalStr | None = None
    vendor_burden_pct: DecimalStr | None = None
    rate_unit: RateUnit | None = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class MarginPreviewIn(_In):
    pay_rate: DecimalStr | None = None
    bill_rate: DecimalStr | None = None
    vendor_rate: DecimalStr | None = None
    burden_pct: DecimalStr | None = None
    vendor_burden_pct: DecimalStr | None = None
    currency: str = "INR"


class MarginOut(BaseModel):
    margin_amount: DecimalStr | None
    margin_pct: DecimalStr | None
    below_minimum: bool = False


class StartGateOut(_Out):
    id: uuid.UUID
    placement_id: uuid.UUID
    kind: str
    label: str | None = None
    source_ref: str
    required: bool
    status: str
    detail: dict[str, Any]
    override_id: uuid.UUID | None
    overridable: bool = True
    refreshed_at: datetime | None


class GateOverrideIn(_In):
    gate_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=4000)]


class GateOverrideOut(_Out):
    id: uuid.UUID
    placement_id: uuid.UUID
    gate_ids: list[uuid.UUID]
    reason: str
    status: str
    approval_request_id: uuid.UUID | None
    requested_by: uuid.UUID | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime


class MarkJoinedIn(_In):
    actual_start_date: date


class DropIn(_In):
    reason: DropReason
    note: _Note | None = None


class NoteIn(_In):
    note: _Note | None = None


class TouchpointOut(_Out):
    id: uuid.UUID
    offset_days: int
    due_on: date
    sent_at: datetime | None
    responded_at: datetime | None
    response: str | None
    response_note: str | None


class StatusHistoryOut(_Out):
    id: uuid.UUID
    from_status: str | None
    to_status: str
    actor_kind: str
    actor_id: uuid.UUID | None
    reason: str | None
    note: str | None
    at: datetime


class PreJoiningRow(BaseModel):
    placement_id: uuid.UUID
    human_id: str
    status: str
    candidate_id: uuid.UUID
    candidate_name: str | None
    client_name: str | None
    role_title: str | None
    planned_start_date: date
    days_to_start: int
    onboarding_pct: int | None
    bgv_summary: dict[str, Any] | None
    gates_total: int
    gates_unmet: int
    last_contact_at: datetime | None
    joining_confirmed_at: datetime | None
    dropout_risk: bool
    dropout_risk_rules: list[str]


# ---------------------------------------------------------------- workers
class BankAccountOut(_Out):
    id: uuid.UUID
    holder_name: str | None
    account_number_last4: str | None
    ifsc: str
    bank_name: str | None
    status: str
    source: str
    approval_request_id: uuid.UUID | None
    activated_at: datetime | None
    created_at: datetime


class BankAccountIn(_In):
    holder_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    account_number: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=34)]
    account_number_confirm: Annotated[str, StringConstraints(strip_whitespace=True, max_length=34)]
    ifsc: Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, max_length=11)]
    bank_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class PanIn(_In):
    pan: Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, max_length=10)]
    pan_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class WorkerProfileUpdate(_In):
    emergency_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    emergency_relation: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    emergency_phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    manager_user_id: uuid.UUID | None = None
    default_worker_type: WorkerType | None = None


class WorkerProfileOut(_Out):
    id: uuid.UUID
    candidate_id: uuid.UUID
    worker_human_id: str
    default_worker_type: str | None
    manager_user_id: uuid.UUID | None
    emergency_name: str | None
    emergency_relation: str | None
    emergency_phone: str | None
    pan_last4: str | None
    pan_name: str | None
    pan_status: str


class WorkerOut(BaseModel):
    candidate_id: uuid.UUID
    full_name: str | None
    email: str | None
    profile: WorkerProfileOut | None
    placements: list[PlacementListItem]
    current_placement_id: uuid.UUID | None
    bank_accounts: list[BankAccountOut]
    sections: dict[str, Any]


class UnmaskIn(_In):
    field: Literal["pan", "bank_account"]
    bank_account_id: uuid.UUID | None = None


class UnmaskOut(BaseModel):
    field: str
    value: str
