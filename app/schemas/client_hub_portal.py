"""Client Portal hub schemas (Epic 18, FR-102/FR-103): approvals hub, delegation, team, compliance packs and the
home tiles. Client-only classes registered with the FR-80 guard (never rates, margin, vendor identity, bank /
ID data or internal notes)."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.field_visibility import register_portal_schema
from app.schemas.commercials_portal import ClientPoConsumptionOut

# Epic 16's PO consumption schema is mounted by this epic: register it with the FR-80 guard (additive).
register_portal_schema("client_user")(ClientPoConsumptionOut)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------- approvals hub
@register_portal_schema("client_user")
class SummaryField(BaseModel):
    label: str
    value: str | None


@register_portal_schema("client_user")
class ApprovalItemOut(BaseModel):
    request_id: uuid.UUID
    kind: str
    subject_id: uuid.UUID
    title: str
    subtitle: str | None
    submitted_at: datetime | None
    acting_for_id: uuid.UUID | None
    acting_for_name: str | None
    decide_via: Literal["generic", "subject_endpoint"]
    link: str | None


@register_portal_schema("client_user")
class ApprovalKindOut(BaseModel):
    kind: str
    label: str
    count: int
    items: list[ApprovalItemOut]


@register_portal_schema("client_user")
class ApprovalsHubOut(BaseModel):
    total: int
    kinds: list[ApprovalKindOut]


@register_portal_schema("client_user")
class ApprovalDetailOut(BaseModel):
    item: ApprovalItemOut
    fields: list[SummaryField]
    status: str


class ApprovalDecideIn(_In):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=4000)
    subject_payload: dict[str, Any] | None = None


# ---------------------------------------------------------------- delegation
@register_portal_schema("client_user")
class DelegationOut(BaseModel):
    id: uuid.UUID
    client_user_id: uuid.UUID
    client_user_name: str | None
    delegate_client_user_id: uuid.UUID
    delegate_name: str | None
    starts_on: date
    ends_on: date
    revoked_at: datetime | None
    state: Literal["current", "future", "past", "revoked"]
    mine: bool


class DelegationIn(_In):
    delegate_client_user_id: uuid.UUID
    starts_on: date
    ends_on: date


@register_portal_schema("client_user")
class ColleagueOut(BaseModel):
    id: uuid.UUID
    display_name: str


# ---------------------------------------------------------------- team
@register_portal_schema("client_user")
class ClientTimesheetBrief(BaseModel):
    id: uuid.UUID
    period_start: date
    period_end: date
    status: str


@register_portal_schema("client_user")
class ClientPlacementOut(BaseModel):
    id: uuid.UUID
    human_id: str
    worker_name: str
    role_title: str | None
    start_date: date | None
    end_date: date | None
    status: str
    ending_soon: bool
    timesheet_status: str | None
    pending_actions: list[str] = Field(default_factory=list)


@register_portal_schema("client_user")
class ClientPlacementDetail(ClientPlacementOut):
    work_location_text: str | None
    timesheets: list[ClientTimesheetBrief] = Field(default_factory=list)


# ---------------------------------------------------------------- compliance packs
@register_portal_schema("client_user")
class CompliancePackItemOut(BaseModel):
    id: uuid.UUID
    label: str


@register_portal_schema("client_user")
class CompliancePackOut(BaseModel):
    id: uuid.UUID
    period_month: date
    title: str
    published_at: datetime | None
    items: list[CompliancePackItemOut]


# ---------------------------------------------------------------- home
@register_portal_schema("client_user")
class HomeTeamTile(BaseModel):
    size: int
    ending_within_30_days: int


@register_portal_schema("client_user")
class ClientHomeOut(BaseModel):
    display_name: str
    approvals_pending: int | None = None
    new_submissions: int | None = None
    upcoming_interview_dates: list[date] | None = None
    team: HomeTeamTile | None = None
    invoices_issued: int | None = None
