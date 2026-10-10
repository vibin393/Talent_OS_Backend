"""Platform operations API models (Epic 12: support access, tenant lifecycle, sub-processors, notices,
incidents, AI budget)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

SupportStatus = Literal["pending", "approved", "denied", "revoked", "expired", "cancelled"]
Severity = Literal["info", "low", "medium", "high", "critical"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ support access
class SupportRequestCreate(_In):
    reason: str = Field(min_length=20, max_length=2000)
    ticket_ref: str | None = Field(default=None, max_length=100)
    requested_hours: int = Field(ge=1, le=24)


class SupportGrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    operator_id: uuid.UUID
    operator_name: str | None = None
    reason: str
    ticket_ref: str | None
    requested_hours: int
    status: SupportStatus
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    approved_at: datetime | None
    expires_at: datetime | None
    revoked_by: uuid.UUID | None
    revoked_at: datetime | None
    created_at: datetime


class SupportSessionOut(BaseModel):
    grant_id: uuid.UUID
    tenant_id: uuid.UUID
    expires_at: datetime
    app_url: str


# ================================================================ tenant lifecycle
class TenantReasonIn(_In):
    reason: str = Field(min_length=10, max_length=2000)


class TenantOffboardIn(TenantReasonIn):
    contact_email: EmailStr
    confirm_slug: str = Field(min_length=1, max_length=100)


class TenantExportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    status: str
    sha256: str | None
    size_bytes: int | None
    expires_at: datetime | None
    created_at: datetime


class TenantLifecycleOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    status_reason: str | None
    suspended_at: datetime | None
    offboard_requested_at: datetime | None
    delete_after: datetime | None
    offboard_contact_email: str | None
    deleted_at: datetime | None
    exports: list[TenantExportOut] = Field(default_factory=list)
    ai_budget: int | None = None
    ai_usage_percent: float | None = None


class AIBudgetIn(_In):
    monthly_token_budget: int | None = Field(ge=0)


class DeletionCertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str
    deleted_at: datetime
    row_counts: dict[str, Any]
    files_deleted: int
    sha256: str


# ================================================================ sub-processors
class SubProcessorIn(_In):
    name: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=2000)
    data_categories: list[str] = Field(default_factory=list, max_length=50)
    processing_location: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=1, max_length=100)
    website: str | None = Field(default=None, max_length=300)
    effective_from: datetime
    emergency: bool = False
    emergency_reason: str | None = Field(default=None, max_length=2000)


class SubProcessorUpdate(_In):
    purpose: str | None = Field(default=None, max_length=2000)
    data_categories: list[str] | None = None
    processing_location: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=100)
    website: str | None = Field(default=None, max_length=300)
    status: Literal["planned", "active", "retired"] | None = None


class SubProcessorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    purpose: str
    data_categories: list[str]
    processing_location: str
    country: str
    website: str | None
    status: str
    effective_from: datetime
    notice_sent_at: datetime | None


# ================================================================ notices
class NoticeCreate(_In):
    tenant_id: uuid.UUID | None = None
    audience: Literal["admins", "all_users"] = "admins"
    kind: Literal["incident", "sub_processor", "maintenance"] = "maintenance"
    severity: Severity = "info"
    title: str = Field(min_length=1, max_length=200)
    body_md: str = Field(min_length=1, max_length=20000)
    expires_at: datetime | None = None


class PlatformNoticeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    audience: str
    kind: str
    severity: str
    title: str
    body_md: str
    expires_at: datetime | None
    created_at: datetime
    acknowledged: bool = False


# ================================================================ incidents
class IncidentCreate(_In):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=20000)
    severity: Severity = "high"
    confirmed_at: datetime
    details: dict[str, Any] = Field(
        default_factory=dict, description="nature, data_categories, mitigation, contact (template variables)"
    )


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str
    severity: str
    confirmed_at: datetime
    status: str
    details: dict[str, Any]
    created_at: datetime


class IncidentNotifyIn(_In):
    tenant_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)
    template_key: Literal["incident_notice"] = "incident_notice"


class IncidentNotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    incident_id: uuid.UUID
    tenant_id: uuid.UUID
    notice_id: uuid.UUID | None
    subject: str
    sent_at: datetime
    recipient_count: int
    late: bool
    acknowledged_count: int = 0
