# ruff: noqa: E501
"""Internal mobility API schemas (Epic 41, FR-209..FR-212). Worker-safe shapes live in ``schemas/mobility_portal``.

Board payloads never carry a client name, rates / compensation or the hiring-manager contact.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

EmployeeKind = Literal["user", "worker"]
SubjectKind = Literal["user", "worker"]
MobilityStatus = Literal[
    "applied", "manager_approval", "approved", "manager_rejected", "withdrawn", "transfer_confirmed"
]
Grade = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ settings
class MobilitySettingsOut(BaseModel):
    board_enabled: bool
    worker_board_enabled: bool
    screen_before_manager: bool
    fallback_approver_role_key: str
    min_tenure_default_months: int | None
    disclosure_text: str


class MobilitySettingsPatch(_In):
    board_enabled: bool | None = None
    worker_board_enabled: bool | None = None
    screen_before_manager: bool | None = None
    fallback_approver_role_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)] | None = None
    min_tenure_default_months: Annotated[int, Field(ge=0, le=600)] | None = None
    disclosure_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=2000)] | None = None


# ================================================================ eligibility rules / profiles
class OpeningRulesIn(_In):
    min_tenure_months: Annotated[int, Field(ge=0, le=600)] | None = None
    grades: list[Grade] | None = None
    locations: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]] | None = None
    business_unit_ids: list[uuid.UUID] | None = None
    note: Annotated[str, StringConstraints(max_length=2000)] | None = None


class OpeningRulesOut(BaseModel):
    demand_id: uuid.UUID
    visibility: str
    min_tenure_months: int | None
    grades: list[str] | None
    locations: list[str] | None
    business_unit_ids: list[uuid.UUID] | None
    note: str | None
    on_board: bool


class ProfileIn(_In):
    subject_kind: SubjectKind
    subject_id: uuid.UUID
    grade: Grade | None = None
    location: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class ProfileOut(BaseModel):
    id: uuid.UUID
    subject_kind: SubjectKind
    subject_id: uuid.UUID
    subject_name: str | None
    subject_email: str | None
    grade: str | None
    location: str | None
    updated_at: datetime


class ProfileImportRow(BaseModel):
    line: int
    email: str
    subject_kind: SubjectKind | None
    subject_id: uuid.UUID | None
    grade: str | None
    location: str | None
    error: str | None


class ProfileImportOut(BaseModel):
    validate_only: bool
    rows: list[ProfileImportRow]
    applied: int
    errors: int


# ================================================================ board
class OpeningSkill(BaseModel):
    name: str
    min_proficiency: int | None = None


class OpeningOut(BaseModel):
    """One board card (internal Users and Workers share the shape; no internal / commercial fields)."""

    demand_id: uuid.UUID
    title: str
    location_text: str | None
    work_mode: str | None
    employment_type: str | None
    skills: list[OpeningSkill]
    visibility: Literal["internal", "both"]
    opened_at: datetime
    closes_at: datetime | None
    eligible: bool
    reasons: list[str]
    fit_pct: int | None
    fit_reasons: list[str]
    application_id: uuid.UUID | None = None  # the Employee's own internal application (overlay id), if any
    application_status: MobilityStatus | None = None


class OpeningDetailOut(OpeningOut):
    summary_html: str
    disclosure_text: str
    can_apply: bool


class OpeningPage(BaseModel):
    items: list[OpeningOut]
    total: int
    page: int
    page_size: int
    board_enabled: bool


class ProfilePreviewOut(BaseModel):
    """The Candidate profile an internal User applies with (linked, found by email, or to be created)."""

    candidate_id: uuid.UUID | None
    will_create: bool
    full_name: str
    email: str
    current_title: str | None
    skills: list[str]


class ApplyIn(_In):
    disclosure_ack: bool
    cover_note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] | None = None


class MyApplicationOut(BaseModel):
    """The Employee's view of their own internal application."""

    id: uuid.UUID
    demand_id: uuid.UUID
    demand_title: str
    status: MobilityStatus
    stage_name: str | None  # only after manager approval
    rejection_reason: str | None
    submitted_at: datetime
    decided_at: datetime | None
    can_withdraw: bool


# ================================================================ recruiter / manager views
class TransferOut(BaseModel):
    id: uuid.UUID
    mobility_application_id: uuid.UUID
    employee_kind: EmployeeKind
    candidate_id: uuid.UUID
    user_id: uuid.UUID | None
    from_placement_id: uuid.UUID | None
    to_placement_id: uuid.UUID | None
    to_placement_human_id: str | None = None
    effective_date: date
    previous_last_working_day: date | None
    from_team_id: uuid.UUID | None
    to_team_id: uuid.UUID | None
    to_role_title: str | None
    confirmed_by: uuid.UUID
    confirmed_at: datetime
    note: str | None


class InternalApplicationOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    application_human_id: str | None
    demand_id: uuid.UUID
    demand_title: str | None
    candidate_id: uuid.UUID
    candidate_name: str | None
    employee_kind: EmployeeKind
    user_id: uuid.UUID | None
    current_placement_id: uuid.UUID | None
    status: MobilityStatus
    stage_key: str | None
    approval_request_id: uuid.UUID | None
    manager_user_id: uuid.UUID | None
    manager_name: str | None
    rejection_reason: str | None
    cover_note: str | None
    submitted_at: datetime
    decided_at: datetime | None
    transfer: TransferOut | None = None


class CurrentPlacementOut(BaseModel):
    id: uuid.UUID
    human_id: str
    status: str
    role_title: str | None
    actual_start_date: date | None
    planned_start_date: date
    end_date: date | None
    legal_entity_id: uuid.UUID
    worker_type: str | None
    engagement_kind: str | None
    currency: str
    rate_unit: str | None = None
    pay_rate: Decimal | None = None  # only with compensation:view
    bill_rate: Decimal | None = None  # only with compensation:view
    burden_pct: Decimal | None = None  # only with compensation:view
    open_offboarding: bool
    open_extension: bool


class TransferContextOut(BaseModel):
    mobility_application_id: uuid.UUID
    employee_kind: EmployeeKind
    status: MobilityStatus
    stage_key: str | None
    can_confirm: bool
    blocking_reason: str | None
    demand_id: uuid.UUID
    demand_title: str | None
    client_id: uuid.UUID | None
    current_placement: CurrentPlacementOut | None
    earliest_effective_date: date


Money = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]


class TransferConfirmIn(_In):
    effective_date: date
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    to_role_title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    to_team_id: uuid.UUID | None = None
    # Workers: the new Placement's commercial terms (default: the current Placement's).
    legal_entity_id: uuid.UUID | None = None
    worker_type: Literal["employee", "contractor", "vendor_contractor"] | None = None
    engagement_kind: Literal["contract", "contract_to_hire", "permanent", "freelance"] | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    rate_unit: Literal["hour", "day", "month", "fixed"] | None = None
    pay_rate: Money | None = None
    bill_rate: Money | None = None
    burden_pct: Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)] | None = None
    work_location_text: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    account_manager_user_id: uuid.UUID | None = None


class FillRateOut(BaseModel):
    filled_internal: int
    filled_total: int
    rate_pct: float | None
