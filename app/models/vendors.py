"""Vendors (Epic 13 contract hub ``vendors``; Epic 17 columns and tables, branch ``p2_vendors``).

Org RLS: ``vendors`` uses ``vendor_col="id"`` (a vendor session sees only its own row, 200_03); every other
vendor-bearing table here uses ``vendor_col="vendor_id"``. ``vendor_candidate_ownerships``,
``vendor_document_requirements`` and ``vendor_settings`` are internal-only (the Vendor Portal never reads
them).

* ``vendor_submissions`` is the **only** candidate data the Vendor Portal reads (AD-26): a snapshot made by
  the
  vendor; ``candidate_id`` is internal and never serialised to vendor schemas.
* PAN and bank account numbers are ``EncryptedStr`` (AD-35) with ``_last4`` / ``_bidx`` companions.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
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
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted import EncryptedStr, Sealed
from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

VENDOR_STATUSES = ("prospective", "onboarding", "active", "suspended", "inactive")
VENDOR_TIERS = ("gold", "silver", "bronze", "custom")
VENDOR_DOCUMENT_CATEGORIES = ("agreement", "insurance", "registration", "other")
BANK_ACCOUNT_STATUSES = ("pending", "active", "rejected", "inactive")
RELEASE_STATUSES = ("active", "paused", "withdrawn", "closed")
OPEN_RELEASE_STATUSES = ("active", "paused")
RATE_UNITS = ("hour", "day", "month", "year", "fixed")
SUBMISSION_STATUSES = ("submitted", "under_review", "accepted", "rejected", "duplicate", "withdrawn")
DUPLICATE_CATEGORIES = ("owned_by_other_vendor", "existing_candidate", "submitted_by_other_vendor", "other")
DOC_UPLOAD_STATUSES = ("pending_review", "reviewed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class Vendor(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "vendors"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", VENDOR_STATUSES), name="status"),
        CheckConstraint(f"tier IS NULL OR {_in('tier', VENDOR_TIERS)}", name="tier"),
    )
    __audit_exclude__ = ("pan", "pan_bidx")

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'prospective'"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 17 (204_01)
    pan: Mapped[Sealed | None] = mapped_column(EncryptedStr(), nullable=True)
    pan_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    pan_bidx: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    primary_contact: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    services: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    skill_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    regions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    tier: Mapped[str | None] = mapped_column(String(10), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    inactive_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class VendorGstin(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "vendor_gstins"
    __table_args__ = (
        UniqueConstraint("vendor_id", "gstin"),
        _tfk("vendor_id", "vendors", "fk_vendor_gstins_vendor_id_vendors", "CASCADE"),
        Index("ix_vendor_gstins_vendor_id", "vendor_id"),
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    gstin: Mapped[str] = mapped_column(CHAR(15), nullable=False)
    state_code: Mapped[str] = mapped_column(CHAR(2), nullable=False)


class VendorBankAccount(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """A change is a ``pending`` row decided through the approval engine (subject ``vendor_bank_change``)."""

    __tablename__ = "vendor_bank_accounts"
    __table_args__ = (
        CheckConstraint(_in("status", BANK_ACCOUNT_STATUSES), name="status"),
        _tfk("vendor_id", "vendors", "fk_vendor_bank_accounts_vendor_id_vendors", "CASCADE"),
        Index("ix_vendor_bank_accounts_vendor_id", "vendor_id"),
    )
    __audit_exclude__ = ("account_no", "account_no_bidx")

    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_name: Mapped[str] = mapped_column(String(200), nullable=False)
    account_no: Mapped[Sealed] = mapped_column(EncryptedStr(), nullable=False)
    account_no_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    account_no_bidx: Mapped[str] = mapped_column(String(32), nullable=False)
    ifsc: Mapped[str] = mapped_column(String(11), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'pending'"))
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class VendorDocumentRequirement(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "vendor_document_requirements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "category"),
        CheckConstraint(_in("category", VENDOR_DOCUMENT_CATEGORIES), name="category"),
    )

    category: Mapped[str] = mapped_column(String(20), nullable=False)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class VendorDocumentUpload(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """A document (version) uploaded from the Vendor Portal, pending the Vendor Manager's review."""

    __tablename__ = "vendor_document_uploads"
    __table_args__ = (
        CheckConstraint(_in("status", DOC_UPLOAD_STATUSES), name="status"),
        _tfk("vendor_id", "vendors", "fk_vendor_document_uploads_vendor_id_vendors", "CASCADE"),
        Index("ix_vendor_document_uploads_vendor_id", "vendor_id"),
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(15), nullable=False, server_default=text("'pending_review'"))
    uploaded_by_vendor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VendorSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "vendor_settings"
    __table_args__ = (UniqueConstraint("tenant_id"),)

    ownership_window_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("90"))
    duplicate_activity_months: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("6"))
    suspension_grace_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("7"))
    mandatory_document_kinds: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{agreement,registration}'")
    )
    scorecard_visible_to_vendor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    vendor_labels: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    notice_link_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    require_bench_review_for_vendor_release: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class VendorRelease(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """A Demand released to one Vendor. The snapshot columns are what the Vendor Portal reads (never
    ``demands``)."""

    __tablename__ = "vendor_releases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", RELEASE_STATUSES), name="status"),
        CheckConstraint("max_vendor_rate IS NULL OR max_vendor_rate >= 0", name="max_rate"),
        CheckConstraint("submission_limit IS NULL OR submission_limit > 0", name="limit"),
        _tfk("vendor_id", "vendors", "fk_vendor_releases_vendor_id_vendors"),
        _tfk("demand_id", "demands", "fk_vendor_releases_demand_id_demands"),
        Index("ix_vendor_releases_vendor_id", "vendor_id"),
        Index("ix_vendor_releases_demand_id", "demand_id"),
        Index(
            "uq_vendor_releases_open",
            "demand_id",
            "vendor_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'paused')"),
        ),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    reveal_client: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    max_vendor_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'INR'"))
    submission_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    released_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    released_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paused_by_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # snapshot (refreshed on DemandRequirementsChanged)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    positions: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    skills: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    experience_range: Mapped[str | None] = mapped_column(String(40), nullable=True)
    jd_public_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_display_name: Mapped[str] = mapped_column(String(300), nullable=False)


class VendorSubmission(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "vendor_submissions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", SUBMISSION_STATUSES), name="status"),
        CheckConstraint(
            f"duplicate_reason_category IS NULL OR {_in('duplicate_reason_category', DUPLICATE_CATEGORIES)}",
            name="duplicate_category",
        ),
        CheckConstraint("vendor_rate >= 0", name="vendor_rate"),
        _tfk(
            "vendor_release_id", "vendor_releases", "fk_vendor_submissions_vendor_release_id_vendor_releases"
        ),
        _tfk("vendor_id", "vendors", "fk_vendor_submissions_vendor_id_vendors"),
        _tfk("demand_id", "demands", "fk_vendor_submissions_demand_id_demands"),
        _tfk("candidate_id", "candidates", "fk_vendor_submissions_candidate_id_candidates", "SET NULL"),
        _tfk(
            "application_id", "applications", "fk_vendor_submissions_application_id_applications", "SET NULL"
        ),
        Index("ix_vendor_submissions_vendor_id", "vendor_id"),
        Index("ix_vendor_submissions_release_id", "vendor_release_id"),
        Index("ix_vendor_submissions_demand_id", "demand_id"),
        Index("ix_vendor_submissions_application_id", "application_id"),
        Index("ix_vendor_submissions_email", "tenant_id", "email"),
    )
    __candidate_pii__ = True
    __audit_exclude__ = ("full_name", "email", "phone_e164", "current_location", "reason_text_internal")

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    vendor_release_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submitted_by_vendor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # snapshot made by the vendor (nulled on erasure)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    total_experience_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    availability_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vendor_rate: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    resume_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resume_draft_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    attestation_text: Mapped[str] = mapped_column(Text, nullable=False)
    attested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False, server_default=text("'submitted'"))
    duplicate_reason_category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reject_reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason_text_internal: Mapped[str | None] = mapped_column(Text, nullable=True)
    vendor_visible_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    duplicate_findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    notice_consent: Mapped[str | None] = mapped_column(String(10), nullable=True)  # granted | withdrawn
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    progress_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    progress_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    progress_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_interview_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    review_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VendorSubmissionEvent(UUIDPk, TenantMixin, Base):
    """Vendor-visible timeline of a submission (status / label changes, no reason text)."""

    __tablename__ = "vendor_submission_events"
    __table_args__ = (
        _tfk("submission_id", "vendor_submissions", "fk_vendor_submission_events_submission_id", "CASCADE"),
        Index("ix_vendor_submission_events_submission_id", "submission_id"),
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VendorCandidateOwnership(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """Internal only (never vendor-visible): which vendor owns a candidate for a client, for how long."""

    __tablename__ = "vendor_candidate_ownerships"
    __table_args__ = (
        _tfk("vendor_id", "vendors", "fk_vendor_candidate_ownerships_vendor_id_vendors"),
        _tfk(
            "candidate_id", "candidates", "fk_vendor_candidate_ownerships_candidate_id_candidates", "CASCADE"
        ),
        _tfk("vendor_submission_id", "vendor_submissions", "fk_vendor_candidate_ownerships_submission_id"),
        Index("ix_vendor_candidate_ownerships_candidate_id", "candidate_id"),
        Index(
            "uq_vendor_candidate_ownerships_active",
            "tenant_id",
            "client_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    vendor_submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
