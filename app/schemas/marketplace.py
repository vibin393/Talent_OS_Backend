# ruff: file-ignore[line-too-long]
"""Installation DTOs (Epic 66 minimal install contract; Epic 69 extends additively)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

InstallationStatus = Literal[
    "pending_consent",
    "pending_approval",
    "active",
    "degraded",
    "reconsent_required",
    "suspended",
    "uninstalled",
]


class InstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["app", "connector"]
    app_id: uuid.UUID | None = None
    connector_key: str | None = Field(default=None, max_length=40)
    scopes: list[str] = Field(default_factory=list)
    sensitive_purposes: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    # Write-only: stored in the vault, never returned.
    secrets: dict[str, SecretStr] = Field(default_factory=dict)
    legal_entity_ids: list[uuid.UUID] = Field(default_factory=list)
    accountable_user_id: uuid.UUID | None = None
    display_name: str | None = Field(default=None, max_length=200)
    accept_manifest_sha256: str = Field(min_length=64, max_length=64)


class ConfigureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any] | None = None
    secrets: dict[str, SecretStr] | None = None
    accountable_user_id: uuid.UUID | None = None
    display_name: str | None = Field(default=None, max_length=200)
    # Epic 69 (additive): add / remove Legal Entities of a legal-entity-scoped connector.
    legal_entity_ids: list[uuid.UUID] | None = None


class InstallationQuery(BaseModel):
    kind: Literal["app", "connector"] | None = None
    status: InstallationStatus | None = None
    # Epic 69 (additive): the listing category filter.
    category: str | None = Field(default=None, max_length=30)
    include_uninstalled: bool = False
    limit: int = Field(50, ge=1, le=200)
    cursor: str | None = None


class SecretMetaOut(BaseModel):
    name: str
    has_secret: bool
    fingerprint: str | None = None
    rotated_at: datetime | None = None


class InstallationOut(BaseModel):
    id: uuid.UUID
    kind: str
    app_id: uuid.UUID | None
    connector_key: str | None
    instance_no: int
    display_name: str
    status: InstallationStatus
    manifest_version: int
    manifest_sha256: str
    pending_manifest_version: int | None
    granted_scopes: list[str]
    sensitive_purposes: dict[str, str]
    granted_by: uuid.UUID | None
    granted_at: datetime | None
    accountable_user_id: uuid.UUID | None
    config: dict[str, Any]
    legal_entity_ids: list[uuid.UUID]
    health_status: str
    last_error_code: str | None
    config_ids: list[uuid.UUID] = Field(default_factory=list)
    created_at: datetime
    # Epic 69 (additive; never a secret value).
    secrets: list[SecretMetaOut] = Field(default_factory=list)
    suspended_reason: str | None = None
    health_checked_at: datetime | None = None
    category: str | None = None
    listing_slug: str | None = None
    last_sync_at: datetime | None = None
    approval_request_id: uuid.UUID | None = None
    test: dict[str, Any] | None = None


# ================================================================ Epic 69: catalogue, listings, policy, processors
ListingKind = Literal["connector", "legacy_connector", "calendar", "app"]
PublisherKindLit = Literal["platform", "partner", "tenant"]
InstallState = Literal[
    "not_installed", "installed", "configured", "pending_approval", "reconsent_required", "degraded", "suspended",
    "pending_consent",
]  # fmt: skip


class ListingQuery(BaseModel):
    category: str | None = Field(default=None, max_length=30)
    publisher: PublisherKindLit | None = None
    q: str | None = Field(default=None, max_length=100)
    installed: bool | None = None
    page: int = Field(1, ge=1, le=100)
    page_size: int = Field(50, ge=1, le=200)


class ScopeLineOut(BaseModel):
    key: str
    reason: str
    sensitive: bool


class DataFlowLineOut(BaseModel):
    direction: str
    entity: str
    fields: list[str]
    purpose: str
    destination_country: str
    personal_data: bool


class ListingVersionLineOut(BaseModel):
    version_no: int
    published_at: datetime | None
    changelog: str | None


class ListingCardOut(BaseModel):
    slug: str
    kind: ListingKind
    name: str
    tagline: str
    category: str
    publisher_kind: PublisherKindLit
    publisher_badge: str
    logo_url: str | None = None
    install_state: InstallState
    install_allowed: bool
    not_allowed_reason: str | None = None
    installation_id: uuid.UUID | None = None
    manage_path: str | None = None
    featured: bool = False


class ListingPageOut(BaseModel):
    data: list[ListingCardOut]
    total: int
    page: int
    page_size: int


class ListingOut(ListingCardOut):
    description_md: str
    screenshots: list[str] = Field(default_factory=list)
    scopes: list[ScopeLineOut] = Field(default_factory=list)
    data_flows: list[DataFlowLineOut] = Field(default_factory=list)
    egress_hosts: list[str] = Field(default_factory=list)
    supported_countries: list[str] = Field(default_factory=list)
    pricing_note: str | None = None
    support_email: str | None = None
    support_url: str | None = None
    versions: list[ListingVersionLineOut] = Field(default_factory=list)
    last_reviewed_at: datetime | None = None
    connector_key: str | None = None
    app_id: uuid.UUID | None = None
    manifest_sha256: str | None = None
    manifest_version: int | None = None
    config_schema: dict[str, Any] | None = None
    secret_fields: list[dict[str, Any]] = Field(default_factory=list)
    legal_entity_scoped: bool = False
    erasure_supported: bool = False
    sync_jobs: list[dict[str, Any]] = Field(default_factory=list)
    auth: str | None = None


class TestResultOut(BaseModel):
    ok: bool
    code: str | None = None
    message: str | None = None
    status: InstallationStatus
    health_status: str


class UpgradeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accept_manifest_sha256: str = Field(min_length=64, max_length=64)


class UpgradeDiffOut(BaseModel):
    installation_id: uuid.UUID
    from_version: int
    to_version: int | None
    manifest_sha256: str | None
    added_scopes: list[str] = Field(default_factory=list)
    added_flows: list[DataFlowLineOut] = Field(default_factory=list)
    added_hosts: list[str] = Field(default_factory=list)
    requires_reconsent: bool = False


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class UninstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    propagate_erasure: bool = False


class UserAuthorizationsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1, max_length=100)
    resource: Literal["api", "mcp"] = "api"


class HealthLogOut(BaseModel):
    checked_at: datetime
    trigger: str
    result: str
    error_code: str | None
    latency_ms: int | None


class PolicyOut(BaseModel):
    publisher_kinds: list[str]
    private_apps: bool
    user_self_authorize: bool
    sensitive_approval_chain_id: uuid.UUID | None
    affected_installations: list[InstallationOut] = Field(default_factory=list)


class ProcessorOut(BaseModel):
    name: str
    source: Literal["platform", "installed"]
    publisher: str | None = None
    category: str | None = None
    purpose: str | None = None
    countries: list[str] = Field(default_factory=list)
    installed_on: datetime | None = None
    status: str | None = None
    installation_id: uuid.UUID | None = None


class DataFlowRow(BaseModel):
    installation_id: uuid.UUID
    name: str
    direction: str
    entity: str
    fields: list[str]
    purpose: str
    destination_country: str
    personal_data: bool
    granted_at: datetime


class DataFlowQuery(BaseModel):
    installation_id: uuid.UUID | None = None
    personal_data: bool | None = None
    destination_country: str | None = Field(default=None, min_length=2, max_length=2)


# ---------------------------------------------------------------- partner submissions (used by Epic 74)
class ListingDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9-]{2,60}$")
    name: str = Field(min_length=2, max_length=120)
    tagline: str = Field(min_length=2, max_length=200)
    category: str = Field(default="other", max_length=30)
    supported_countries: list[str] = Field(default_factory=lambda: ["IN"], max_length=50)
    pricing_note: str | None = Field(default=None, max_length=300)
    support_email: str | None = Field(default=None, max_length=254)
    support_url: str | None = Field(default=None, max_length=2000)
    logo_file_key: str | None = Field(default=None, max_length=300)


class VersionDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description_md: str = Field(default="", max_length=8000)
    screenshots: list[str] = Field(default_factory=list, max_length=5)
    privacy_url: str | None = Field(default=None, max_length=2000)
    dpa_url: str | None = Field(default=None, max_length=2000)
    india_transfer_statement: str | None = Field(default=None, max_length=4000)
    security_questionnaire: dict[str, Any] | None = None
    changelog: str | None = Field(default=None, max_length=2000)


class CheckResultOut(BaseModel):
    key: str
    passed: bool
    detail: str | None = None


class VersionOut(BaseModel):
    id: uuid.UUID
    listing_id: uuid.UUID
    version_no: int
    status: str
    manifest_version: int
    manifest_sha256: str
    description_md: str
    screenshots: list[str]
    privacy_url: str | None
    dpa_url: str | None
    india_transfer_statement: str | None
    changelog: str | None
    check_results: list[CheckResultOut]
    submitted_at: datetime | None
    decided_at: datetime | None
    decision_message: str | None
    published_at: datetime | None
    messages: list[str] = Field(default_factory=list)


class PartnerListingOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    tagline: str
    category: str
    status: str
    app_id: uuid.UUID | None
    current_version_id: uuid.UUID | None
    install_count: int
    last_reviewed_at: datetime | None
