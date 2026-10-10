"""Career Portal & Candidate Portal tables (Epic 9).

Tenant-owned with forced RLS (migrations 20261001_90 .. 92):

* ``career_sites`` -- one row per tenant (created lazily): branding, about text, candidate stage labels.
* ``apply_sessions`` -- a public apply in progress: consents captured *before* upload / parse, the stored
  resume, parse result and prefill. Holds personal data only until submit (cleared) or expiry (purged).
* ``application_answers`` -- the posting's apply questions (snapshot) and the candidate's answers.
* ``data_export_jobs`` -- candidate self-service data exports (FR-51), each linked to an ``access`` DSR.

Platform table (no RLS, no PII): ``apply_session_lookups`` -- token hash -> (tenant, session).
The minimal DSR intake uses Epic 12's ``dsr_requests`` (``app.models.privacy``).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

PARSE_STATUSES = ("pending", "parsed", "failed_manual", "unavailable", "skipped_disabled", "skipped_consent")
EXPORT_STATUSES = ("queued", "running", "ready", "failed", "expired")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class CareerSite(TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "career_sites"
    __audit_entity_id__ = "tenant_id"
    __audit_exclude__ = ("about_html",)
    __table_args__ = (
        CheckConstraint("brand_color IS NULL OR brand_color ~ '^#[0-9A-Fa-f]{6}$'", name="brand_color"),
        CheckConstraint("about_md IS NULL OR char_length(about_md) <= 10000", name="about_len"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    logo_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    header_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    brand_color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    about_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    about_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    privacy_contact_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    candidate_stage_labels: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    apply_requires_phone: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class ApplySession(UUIDPk, TenantMixin, Timestamps, Base):
    """Not audited (holds a resume parse and contact details only until submit / expiry)."""

    __tablename__ = "apply_sessions"
    __table_args__ = (
        CheckConstraint(
            f"parse_status IS NULL OR {_in('parse_status', PARSE_STATUSES)}", name="parse_status"
        ),
        Index("ix_apply_sessions_expires_at", "expires_at"),
        Index("ix_apply_sessions_application_id", "application_id"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    posting_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    consents: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    resume_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    upload_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    resume_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    parse_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    parsed_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    prefill: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Confirmation email recipient: kept only until ``send_apply_confirmation`` has sent it.
    confirmation_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    # Epic 70 (D-18, migration 20261010_504_01): careers attribution tag (``src`` / ``ref`` of the apply link),
    # resolved on submit by ``portal.apply.register_apply_source_resolver`` (Epic 75 reuses it).
    src: Mapped[str | None] = mapped_column(String(40), nullable=True)
    src_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ApplySessionLookup(Base):
    """Platform table: apply token hash -> tenant + session id. No PII."""

    __tablename__ = "apply_session_lookups"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApplicationAnswer(UUIDPk, TenantMixin, Timestamps, Base):
    """Answers to a posting's apply questions. Not audited (free text about the candidate); erased by the
    ``portal.account`` handler. ``maps_to`` answers are proposals only -- never written to the candidate."""

    __tablename__ = "application_answers"
    __candidate_pii__ = True
    __table_args__ = (UniqueConstraint("application_id"),)

    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    questions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DataExportJob(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "data_export_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_data_export_jobs_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_in("status", EXPORT_STATUSES), name="status"),
        Index("ix_data_export_jobs_candidate_id", "candidate_id"),
        Index("ix_data_export_jobs_status", "status"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'queued'"))
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dsr_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
