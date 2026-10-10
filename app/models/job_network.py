# ruff: noqa: E501
"""Cross-tenant Job Network (Epic 75, AD-74/AD-87; migrations ``20261020_509_01_job_network`` / ``509_02_jn_stats``).

Jobs only -- **no Candidate data ever crosses tenants** (DPDP decision 2). The ``p5-network-platform`` import-linter
contract forbids every module except ``app.services.job_network`` and its routers from importing this module.

Platform tables (``core.tenancy.PLATFORM_TABLES``; no RLS, explicit ``talent_app`` grants). They hold **only
careers-public posting data plus opaque routing keys**; ``tenant_id`` / ``member_id`` / ``posting_ref`` are routing and
isolation only and are never serialised by any public schema (``tests/job_network/test_no_personal_data.py`` freezes the
column allow-list, rejects personal-data column names and forbids FKs to tenant tables):

* ``network_members`` -- one row per joined Tenant (the network's view of an agency).
* ``network_listings`` -- the public index (one row per listed Demand Posting).
* ``network_listing_stats`` -- daily aggregates (counts only; no IP / UA / cookie / referrer / session).
* ``network_reports`` -- visitor reports (``ip_hash`` for rate limiting only, purged at 30 days; no reporter identity).
* ``network_moderation_actions`` -- append-only audit of platform decisions (``GRANT SELECT, INSERT`` only).

Tenant tables (standard conventions + RLS):

* ``job_network_settings`` -- one row per Tenant (opt-in, listing mode, excluded clients).
* ``job_network_choices`` -- per-Demand include / exclude.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

# ---------------------------------------------------------------- enums
MEMBER_STATUSES = ("pending", "active", "suspended", "left")
VERIFICATION_STATUSES = ("unverified", "pending", "verified", "rejected")
LISTING_STATUSES = ("pending_checks", "listed", "held", "suspended", "unlisted", "removed")
REPORT_REASONS = ("scam_fee", "fake_job", "offensive", "discriminatory", "wrong_info", "duplicate", "other")
REPORT_STATUSES = ("open", "upheld", "dismissed")
MODERATION_TARGETS = ("listing", "member")
MODERATION_ACTIONS = (
    "verify", "reject_verification", "suspend", "reinstate", "remove", "uphold_report", "dismiss_report",
    "release_hold",
)  # fmt: skip
LIST_MODES = ("all", "choose")
PAY_PERIODS = ("year", "month", "day", "hour")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# ================================================================ platform tables
class NetworkMember(UUIDPk, Timestamps, Base):
    __tablename__ = "network_members"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_network_members_tenant_id"),
        UniqueConstraint("public_slug", name="uq_network_members_public_slug"),
        CheckConstraint(_in("status", MEMBER_STATUSES), name="status"),
        CheckConstraint(_in("verification_status", VERIFICATION_STATUSES), name="verification_status"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    public_slug: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    logo_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    about_text: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    careers_base_url: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'pending'"))
    verification_status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'pending'"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    violation_count: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("0"))
    listing_limit: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("500"))
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NetworkListing(UUIDPk, Base):
    __tablename__ = "network_listings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "posting_ref", name="uq_network_listings_tenant_id_posting_ref"),
        CheckConstraint(_in("status", LISTING_STATUSES), name="status"),
        Index("ix_network_listings_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_network_listings_status_published", "status", text("published_at DESC")),
        Index("ix_network_listings_status_geo", "status", "country", "region", "city"),
        Index("ix_network_listings_skills", "skills", postgresql_using="gin"),
        Index("ix_network_listings_member_status", "member_id", "status"),
        Index(
            "ix_network_listings_listed_published", text("published_at DESC"),
            postgresql_where=text("status = 'listed'"),
        ),
    )

    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("network_members.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    posting_ref: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'pending_checks'"))
    hold_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, server_default=text("'{}'")
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description_html: Mapped[str] = mapped_column(Text(), nullable=False)
    description_text: Mapped[str] = mapped_column(Text(), nullable=False)
    client_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)
    location_hidden: Mapped[bool] = mapped_column(Boolean(), nullable=False, server_default=text("false"))
    work_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    experience_min: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    experience_max: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    skills: Mapped[list[str]] = mapped_column(ARRAY(String(80)), nullable=False, server_default=text("'{}'"))
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    pay_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    pay_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    pay_currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    pay_period: Mapped[str | None] = mapped_column(String(10), nullable=True)
    apply_url: Mapped[str] = mapped_column(Text(), nullable=False)
    posting_url: Mapped[str] = mapped_column(Text(), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    search_tsv: Mapped[str] = mapped_column(TSVECTOR(), nullable=False, server_default=text("''"))
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class NetworkListingStat(Base):
    __tablename__ = "network_listing_stats"
    __table_args__ = (
        Index("ix_network_listing_stats_day", "day"),
    )

    listing_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("network_listings.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date(), primary_key=True)
    impressions: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("0"))
    views: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("0"))
    apply_clicks: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("0"))


class NetworkReport(UUIDPk, Base):
    __tablename__ = "network_reports"
    __table_args__ = (
        CheckConstraint(_in("reason", REPORT_REASONS), name="reason"),
        CheckConstraint(_in("status", REPORT_STATUSES), name="status"),
        Index("ix_network_reports_status_created", "status", "created_at"),
        Index("ix_network_reports_listing_id", "listing_id"),
    )

    listing_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("network_listings.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    details: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    ip_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'open'"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class NetworkModerationAction(UUIDPk, Base):
    __tablename__ = "network_moderation_actions"
    __table_args__ = (
        CheckConstraint(_in("target_type", MODERATION_TARGETS), name="target_type"),
        CheckConstraint(_in("action", MODERATION_ACTIONS), name="action"),
        Index("ix_network_moderation_actions_target", "target_type", "target_id"),
    )

    target_type: Mapped[str] = mapped_column(String(10), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    operator_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# ================================================================ tenant tables
class JobNetworkSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "job_network_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_job_network_settings_tenant_id"),
        UniqueConstraint("tenant_id", "id", name="uq_job_network_settings_tenant_id_id"),
        CheckConstraint(_in("list_mode", LIST_MODES), name="list_mode"),
    )

    joined: Mapped[bool] = mapped_column(Boolean(), nullable=False, server_default=text("false"))
    list_mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'choose'"))
    terms_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terms_accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    show_logo: Mapped[bool] = mapped_column(Boolean(), nullable=False, server_default=text("true"))
    exclude_client_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    member_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class JobNetworkChoice(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "job_network_choices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "demand_id", name="uq_job_network_choices_tenant_id_demand_id"),
        UniqueConstraint("tenant_id", "id", name="uq_job_network_choices_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_job_network_choices_demand_id_demands",
            ondelete="CASCADE",
        ),
        Index("ix_job_network_choices_demand_id", "demand_id"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    posting_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    include: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    chosen_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    chosen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "LISTING_STATUSES",
    "LIST_MODES",
    "MEMBER_STATUSES",
    "MODERATION_ACTIONS",
    "REPORT_REASONS",
    "VERIFICATION_STATUSES",
    "JobNetworkChoice",
    "JobNetworkSettings",
    "NetworkListing",
    "NetworkListingStat",
    "NetworkMember",
    "NetworkModerationAction",
    "NetworkReport",
]
