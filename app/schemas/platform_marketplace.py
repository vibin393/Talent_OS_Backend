"""Platform marketplace review DTOs (Epic 69, spec A7 / Part C platform)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.marketplace import CheckResultOut

CHECKLIST_KEYS = (
    "purpose_matches_scopes",
    "flows_match_sandbox_traffic",
    "india_transfer_statement",
    "security_questionnaire",
    "support_commitments",
    "no_human_only_automation",
)


class QueueItemOut(BaseModel):
    version_id: uuid.UUID
    listing_id: uuid.UUID
    app_name: str
    partner_name: str | None
    version_no: int
    submitted_at: datetime | None
    status: str
    checks_failed: int
    reviewer_id: uuid.UUID | None


class ReviewEntryOut(BaseModel):
    action: str
    operator_id: uuid.UUID
    checklist: dict[str, Any] | None
    message: str | None
    internal_note: str | None
    at: datetime


class ReviewVersionOut(BaseModel):
    version_id: uuid.UUID
    listing_id: uuid.UUID
    listing_slug: str
    listing_status: str
    app_id: uuid.UUID | None
    app_name: str
    partner_name: str | None
    version_no: int
    status: str
    manifest_snapshot: dict[str, Any]
    scopes: list[Any]
    data_flows: list[Any]
    egress_hosts: list[str]
    description_md: str
    privacy_url: str | None
    dpa_url: str | None
    india_transfer_statement: str | None
    security_questionnaire: dict[str, Any] | None
    changelog: str | None
    check_results: list[CheckResultOut]
    conformance_report: dict[str, Any] | None
    checklist: dict[str, Any]
    claimed_by: uuid.UUID | None
    history: list[ReviewEntryOut]


class ChecklistItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    note: str | None = Field(default=None, max_length=1000)


class ChecklistIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: dict[str, ChecklistItemIn]
    internal_note: str | None = Field(default=None, max_length=4000)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(default="", max_length=4000)
    internal_note: str | None = Field(default=None, max_length=4000)


class ListingActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class PlatformListingOut(BaseModel):
    id: uuid.UUID
    slug: str
    kind: str
    name: str
    publisher_kind: str
    partner_name: str | None
    category: str
    status: str
    install_count: int
    last_reviewed_at: datetime | None
    suspended_reason: str | None


class OperatorRolesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: list[Literal["marketplace_reviewer", "network_moderator"]] = Field(
        default_factory=list, max_length=2
    )


class OperatorRolesOut(BaseModel):
    operator_id: uuid.UUID
    roles: list[str]
