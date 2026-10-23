# ruff: file-ignore[line-too-long]
"""Integration Marketplace (Epic 69, AD-81/AD-83; migrations ``20261010_503_01_marketplace`` / ``503_02_mkt_tenant``).

Platform tables (``core.tenancy.PLATFORM_TABLES``; no Tenant data -- ``owner_tenant_id`` of a private listing is the only
tenant reference and is never shown to anyone but that Tenant):

* ``marketplace_listings``: one row per platform connector, legacy P2-P4 connector, calendar connection or partner App.
* ``marketplace_listing_versions``: consent-relevant content per version (manifest snapshot, scopes, data flows, egress
  hosts); immutable after ``submitted`` (service-enforced).
* ``marketplace_reviews``: append-only reviewer actions.

Tenant table: ``installation_health_log`` (not audited, retention class ``connector_payloads``; no personal data).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, Timestamps, UUIDPk

LISTING_KINDS = ("connector", "legacy_connector", "calendar", "app")
PUBLISHER_KINDS = ("platform", "partner", "tenant")
LISTING_STATUSES = ("draft", "published", "suspended", "delisted")
LISTING_CATEGORIES = (
    "job_boards", "hrms", "accounting", "payroll", "calendar", "telephony", "speech_to_text", "assessment", "bgv",
    "esignature", "einvoice", "messaging", "finance_compliance", "bi", "other",
)  # fmt: skip
VERSION_STATUSES = (
    "draft", "submitted", "automated_checks", "in_review", "changes_requested", "approved", "published", "rejected",
    "suspended", "delisted", "superseded",
)  # fmt: skip
REVIEW_ACTIONS = (
    "claim", "checklist", "approve", "request_changes", "reject", "suspend", "reinstate", "delist", "comment",
)  # fmt: skip
HEALTH_TRIGGERS = ("install", "manual", "scheduled", "job_failures")
HEALTH_RESULTS = ("ok", "degraded", "failing")
PLATFORM_ROLES = ("marketplace_reviewer", "network_moderator")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class MarketplaceListing(UUIDPk, Timestamps, Base):
    __tablename__ = "marketplace_listings"
    __table_args__ = (
        CheckConstraint(_in("kind", LISTING_KINDS), name="kind"),
        CheckConstraint(_in("publisher_kind", PUBLISHER_KINDS), name="publisher_kind"),
        CheckConstraint(_in("status", LISTING_STATUSES), name="status"),
        CheckConstraint(_in("category", LISTING_CATEGORIES), name="category"),
        CheckConstraint("slug ~ '^[a-z0-9-]{2,60}$'", name="slug"),
        CheckConstraint(
            "((kind IN ('connector', 'calendar')) = (connector_key IS NOT NULL)) AND "
            "((kind = 'legacy_connector') = (legacy_key IS NOT NULL)) AND ((kind = 'app') = (app_id IS NOT NULL))",
            name="kind_ref",
        ),
        CheckConstraint(
            "((publisher_kind = 'tenant') = (owner_tenant_id IS NOT NULL)) AND "
            "((publisher_kind = 'partner') = (partner_org_id IS NOT NULL)) AND "
            "(kind = 'app' OR publisher_kind = 'platform')",
            name="publisher",
        ),
        UniqueConstraint("slug", name="uq_marketplace_listings_slug"),
        Index(
            "uq_marketplace_listings_connector",
            "connector_key",
            unique=True,
            postgresql_where=text("connector_key IS NOT NULL"),
        ),
        Index(
            "uq_marketplace_listings_legacy",
            "legacy_key",
            unique=True,
            postgresql_where=text("legacy_key IS NOT NULL"),
        ),
        Index(
            "uq_marketplace_listings_app", "app_id", unique=True, postgresql_where=text("app_id IS NOT NULL")
        ),
        Index("ix_marketplace_listings_status_category", "status", "category"),
    )

    slug: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    connector_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    legacy_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    app_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("apps.id", ondelete="RESTRICT", name="fk_marketplace_listings_app_id_apps"),
        nullable=True,
    )
    publisher_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    partner_org_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "partner_orgs.id", ondelete="RESTRICT", name="fk_marketplace_listings_partner_org_id_partner_orgs"
        ),
        nullable=True,
    )
    owner_tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    tagline: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'draft'"))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    supported_countries: Mapped[list[str]] = mapped_column(
        ARRAY(CHAR(2)), nullable=False, server_default=text("'{IN}'")
    )
    pricing_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    support_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    support_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    logo_file_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    manage_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    featured: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    install_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class MarketplaceListingVersion(UUIDPk, Timestamps, Base):
    __tablename__ = "marketplace_listing_versions"
    __table_args__ = (
        CheckConstraint(_in("status", VERSION_STATUSES), name="status"),
        CheckConstraint(
            "submitted_by_kind IS NULL OR submitted_by_kind IN ('developer', 'platform')",
            name="submitted_by_kind",
        ),
        UniqueConstraint("listing_id", "version_no", name="uq_marketplace_listing_versions_no"),
        Index("ix_marketplace_listing_versions_status_submitted", "status", "submitted_at"),
        Index("ix_marketplace_listing_versions_sha", "listing_id", "manifest_sha256"),
    )

    listing_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "marketplace_listings.id", ondelete="CASCADE", name="fk_marketplace_listing_versions_listing_id"
        ),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    manifest_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    scopes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    data_flows: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    egress_hosts: Mapped[list[str]] = mapped_column(
        ARRAY(String(255)), nullable=False, server_default=text("'{}'")
    )
    description_md: Mapped[str] = mapped_column(Text(), nullable=False)
    screenshots: Mapped[list[str]] = mapped_column(
        ARRAY(String(300)), nullable=False, server_default=text("'{}'")
    )
    privacy_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    dpa_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    india_transfer_statement: Mapped[str | None] = mapped_column(Text(), nullable=True)
    security_questionnaire: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    changelog: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    conformance_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    check_results: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    submitted_by_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    submitted_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_message: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MarketplaceReview(UUIDPk, Base):
    __tablename__ = "marketplace_reviews"
    __table_args__ = (
        CheckConstraint(_in("action", REVIEW_ACTIONS), name="action"),
        Index("ix_marketplace_reviews_version_at", "version_id", "at"),
    )

    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "marketplace_listing_versions.id", ondelete="CASCADE", name="fk_marketplace_reviews_version_id"
        ),
        nullable=False,
    )
    operator_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    checklist: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    message: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    internal_note: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class InstallationHealthLog(UUIDPk, TenantMixin, Base):
    __tablename__ = "installation_health_log"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "installation_id"],
            ["app_installations.tenant_id", "app_installations.id"],
            name="fk_installation_health_log_installation_id_inst",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("trigger", HEALTH_TRIGGERS), name="trigger"),
        CheckConstraint(_in("result", HEALTH_RESULTS), name="result"),
        Index("ix_installation_health_log_inst_checked", "installation_id", text("checked_at DESC")),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(12), nullable=False)
    result: Mapped[str] = mapped_column(String(10), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


__all__ = [
    "HEALTH_RESULTS",
    "HEALTH_TRIGGERS",
    "LISTING_CATEGORIES",
    "LISTING_KINDS",
    "LISTING_STATUSES",
    "PLATFORM_ROLES",
    "REVIEW_ACTIONS",
    "VERSION_STATUSES",
    "InstallationHealthLog",
    "MarketplaceListing",
    "MarketplaceListingVersion",
    "MarketplaceReview",
]
