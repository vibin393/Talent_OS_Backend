# ruff: file-ignore[line-too-long]
"""Epic 79 (Phase 5 Governance) API schemas: console read models, data-flow rows, violations, access reviews,
data recipients and go-live readiness. All read models are masked (no secrets, no payloads, no subject ids)."""

from __future__ import annotations

import datetime as dt  # noqa: TC003
import uuid  # noqa: TC003

from pydantic import BaseModel, Field


class ConsoleSummary(BaseModel):
    active: int = 0
    degraded: int = 0
    suspended: int = 0
    pending_reconsents: int = 0
    failing_subscriptions: int = 0
    active_mcp_sessions: int = 0
    open_access_review_items: int = 0
    violations_7d: int = 0
    kill_switch_on: bool = False


class InstallationRow(BaseModel):
    kind: str  # app | connector | api_key
    id: str
    name: str
    publisher: str | None = None  # platform | partner | tenant | None
    version: str | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    sensitive_scopes: list[str] = Field(default_factory=list)
    status: str | None = None
    health_status: str | None = None
    last_activity_at: dt.datetime | None = None
    requests_30d: int = 0
    errors_30d: int = 0
    minutes_30d: int = 0
    accountable_user: str | None = None


class DataFlowRow(BaseModel):
    installation: str
    publisher: str | None = None
    direction: str  # out | in
    entity: str
    fields: list[str] = Field(default_factory=list)
    personal_data: bool = False
    purpose: str | None = None
    destination_country: str | None = None
    since: dt.date | None = None
    subjects_exposed_90d: int = 0
    processor_role: str | None = None
    restricted_transfer: bool = False


class ViolationRow(BaseModel):
    kind: str
    at: dt.datetime | None = None
    installation: str | None = None
    user: str | None = None
    route_or_tool: str | None = None
    error_code: str | None = None


class AccessReviewSettingsOut(BaseModel):
    cadence_months: int
    stale_after_days: int
    auto_revoke_stale: bool
    reviewer_role_key: str | None = None


class AccessReviewSettingsIn(BaseModel):
    cadence_months: int = Field(ge=1, le=12)
    stale_after_days: int = Field(ge=1, le=3650)
    auto_revoke_stale: bool = False
    reviewer_role_key: str | None = None


class AccessReviewOut(BaseModel):
    id: uuid.UUID
    period_start: dt.date
    period_end: dt.date
    status: str
    item_count: int
    decided_count: int
    opened_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None


class AccessReviewItemOut(BaseModel):
    id: uuid.UUID
    kind: str
    subject_id: uuid.UUID
    snapshot: dict[str, object] = Field(default_factory=dict)
    stale: bool = False
    decision: str = "pending"
    note: str | None = None


class AccessDecisionIn(BaseModel):
    decision: str  # confirmed | reduced | revoked
    note: str | None = Field(default=None, max_length=500)


class RevokeIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class DataRecipientOut(BaseModel):
    installation: str
    publisher: str | None = None
    purposes: list[str] = Field(default_factory=list)
    destination_country: str | None = None
    first_at: dt.datetime | None = None
    last_at: dt.datetime | None = None


class EnablementCheckOut(BaseModel):
    key: str
    label: str
    status: str  # ok | warning | blocking
    detail: str | None = None


class FeatureReadinessOut(BaseModel):
    feature: str
    status: str  # ok | warning | blocking
    checks: list[EnablementCheckOut] = Field(default_factory=list)
