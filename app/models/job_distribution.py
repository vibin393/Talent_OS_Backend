# ruff: noqa: E501
"""Job board distribution (Epic 70, FR-360..FR-364; migration ``20261010_504_01_job_boards``). Owner:
``app.services.job_distribution``.

* ``board_postings`` -- one published (or attempted) copy of a careers posting on one board (audited).
* ``board_sets`` -- default board selections per employment type (audited).
* ``board_field_mappings`` -- Talent OS value -> board taxonomy value, per board and kind (audited).
* ``board_applicant_ingests`` -- idempotency + outcome of one board application; never holds personal data.
* ``board_ingest_suppressions`` -- HMAC hashes of erased / withdrawn / do-not-contact emails and phones.
* ``board_credit_snapshots`` -- credits reported by a board (90-day retention).
* ``job_feed_settings`` / ``job_board_settings`` -- one row per tenant, created lazily (audited).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

BOARD_KEYS = ("naukri", "linkedin", "indeed", "job_feed", "fake_job_board", "manual")
BP_STATUSES = ("draft", "publishing", "live", "expiring", "expired", "closed", "failed", "abandoned", "rejected")
OPEN_STATUSES = ("draft", "publishing", "live", "expiring", "failed", "rejected")
CLOSE_REASONS = ("user", "demand_closed", "demand_on_hold", "demand_filled", "posting_unpublished", "expired", "board")
MAPPING_KINDS = ("function", "industry", "location", "experience_band", "salary_band", "employment_type", "education", "default")
INGEST_STATUSES = ("received", "created", "linked", "suppressed", "failed")
SUPPRESSION_REASONS = ("erased", "consent_withdrawn", "do_not_contact")
RESUME_STATUSES = ("stored", "rejected", "missing")
EMPLOYMENT_TYPES = ("contract", "contract_to_hire", "permanent", "freelance")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


class BoardPosting(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "board_postings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id", name="uq_board_postings_human_id"),
        _fk("demand_id", "demands", "fk_board_postings_demand_id_demands", "CASCADE"),
        ForeignKeyConstraint(["posting_id"], ["demand_postings.id"], name="fk_board_postings_posting_id_demand_postings", ondelete="CASCADE"),
        _fk("installation_id", "app_installations", "fk_board_postings_installation_id_inst"),
        _fk("published_by", "users", "fk_board_postings_published_by_users"),
        CheckConstraint(_in("status", BP_STATUSES), name="status"),
        CheckConstraint("close_reason IS NULL OR " + _in("close_reason", CLOSE_REASONS), name="close_reason"),
        CheckConstraint("revision <= local_revision", name="revision"),
        Index(
            "uq_board_postings_open", "tenant_id", "posting_id", "board_key", unique=True,
            postgresql_where=text("status NOT IN ('closed', 'expired', 'abandoned')"),
        ),
        Index("ix_board_postings_demand_id", "demand_id"),
        Index("ix_board_postings_installation_status", "installation_id", "status"),
        Index("ix_board_postings_status_expires", "status", "expires_at"),
    )
    __audit_exclude__ = ("content_sha256",)

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    posting_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    board_key: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'draft'"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    local_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    jd_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    board_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    blocked_fields: Mapped[list[str]] = mapped_column(ARRAY(String(60)), nullable=False, server_default=text("'{}'::varchar(60)[]"))
    board_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    board_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applies_reported: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    manual: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    published_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cost_note: Mapped[str | None] = mapped_column(String(200), nullable=True)


class BoardSet(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "board_sets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("employment_type IS NULL OR " + _in("employment_type", EMPLOYMENT_TYPES), name="employment_type"),
        Index(
            "uq_board_sets_default", "tenant_id", "employment_type", unique=True,
            postgresql_where=text("is_default"), postgresql_nulls_not_distinct=True,
        ),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    employment_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    board_keys: Mapped[list[str]] = mapped_column(ARRAY(String(40)), nullable=False, server_default=text("'{}'::varchar(40)[]"))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class BoardFieldMapping(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "board_field_mappings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "board_key", "kind", "local_value", name="uq_board_field_mappings_value"),
        CheckConstraint(_in("kind", MAPPING_KINDS), name="kind"),
    )

    board_key: Mapped[str] = mapped_column(String(40), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    local_value: Mapped[str] = mapped_column(String(200), nullable=False)
    board_value: Mapped[str] = mapped_column(String(200), nullable=False)
    board_label: Mapped[str | None] = mapped_column(String(200), nullable=True)


class BoardApplicantIngest(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "board_applicant_ingests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "board_key", "board_application_id", name="uq_board_applicant_ingests_app"),
        _fk("board_posting_id", "board_postings", "fk_board_applicant_ingests_bp_id_board_postings", "SET NULL"),
        CheckConstraint(_in("status", INGEST_STATUSES), name="status"),
        CheckConstraint("suppression_reason IS NULL OR " + _in("suppression_reason", SUPPRESSION_REASONS), name="reason"),
        CheckConstraint("resume_status IS NULL OR " + _in("resume_status", RESUME_STATUSES), name="resume"),
        Index("ix_board_applicant_ingests_candidate_id", "candidate_id"),
        Index("ix_board_applicant_ingests_board_posting_id", "board_posting_id"),
    )

    board_posting_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    board_key: Mapped[str] = mapped_column(String(40), nullable=False)
    board_application_id: Mapped[str] = mapped_column(String(200), nullable=False)
    inbound_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'received'"))
    suppression_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_candidate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    resume_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BoardIngestSuppression(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "board_ingest_suppressions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "identifier_hash", name="uq_board_ingest_suppressions_hash"),
        CheckConstraint(_in("kind", ("email", "phone")), name="kind"),
        CheckConstraint(_in("reason", SUPPRESSION_REASONS), name="reason"),
    )

    identifier_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(6), nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)


class BoardCreditSnapshot(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "board_credit_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index("ix_board_credit_snapshots_inst_observed", "installation_id", text("observed_at DESC")),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    credits_left: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class JobFeedSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "job_feed_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", name="uq_job_feed_settings_tenant_id"),
        CheckConstraint(_in("company_name_mode", ("tenant", "client")), name="company_name_mode"),
    )
    __audit_exclude__ = ("feed_token",)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    feed_token: Mapped[str] = mapped_column(String(43), nullable=False)
    include_salary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    company_name_mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'tenant'"))


class JobBoardSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "job_board_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", name="uq_job_board_settings_tenant_id"),
        CheckConstraint("expiry_warning_days BETWEEN 1 AND 14", name="expiry_days"),
    )

    expiry_warning_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    applicant_digest: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    auto_close_on_demand_change: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    default_board_set_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
