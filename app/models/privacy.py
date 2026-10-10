"""Privacy tables (Epic 12; minimal consent model of Epic 6 A4 built here because Epic 6 has not landed).

All tenant-owned with forced RLS (migration 20261001_121):

* ``consent_notices`` -- versioned notice texts per ``(purpose, locale)``; one active version each.
* ``consents`` -- grants/withdrawals by purpose (``client_id`` for ``client_submission``).
* ``dsr_requests`` -- data subject requests (AD-4 state machine in ``services.privacy.dsr``).
* ``legal_holds`` -- block erasure and retention for a candidate.
* ``erasure_runs`` -- one erasure execution (DSR or retention) with per-handler results.
* ``retention_policies`` / ``retention_runs`` / ``retention_run_items`` -- retention preview + execute.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CONSENT_PURPOSES = (
    "data_processing", "ai_processing", "client_submission", "marketing",
    # Epic 13 (R12): Phase 2 purposes (notices are seeded by their owning epics).
    "bgv", "reference_check", "vendor_collection", "whatsapp", "engagement",
    # Epic 36 (migration 20261007_300_01): Phase 3 purposes (notices registered by their owning epics).
    "assessment", "demographic_self_id", "referral", "campus", "immigration", "tracking",
    # Epic 53 (migration 20261008_400_01, AD-67): Phase 4 purposes (notices registered by Epics 57 / 58 / 60 / 62).
    "ai_agent_communication", "voice_screening", "call_recording", "interview_recording", "knowledge_assistant",
    # Epic 66 (migration 20261009_500_01, FR-408): Phase 5 purposes (notices registered by Epics 70 / 75 / 71 / 77).
    "job_board_application", "job_network_application", "hrms_transfer", "portal_copilot",
)  # fmt: skip
CONSENT_STATUSES = ("granted", "withdrawn")
CONSENT_SOURCES = (
    "candidate_link", "candidate_portal", "application", "recruiter_attestation", "import",
    # Epic 13 (R12)
    "vendor_attestation", "vendor_notice", "worker_portal", "placement", "whatsapp_optin", "recipient_link",
    # Epic 36 (300_01)
    "campus_registration", "event_checkin", "referral_link", "self_id_form",
    # Epic 53 (400_01, AD-67)
    "conversation", "voice_consent_link", "interview_invite",
    # Epic 66 (500_01)
    "job_board", "job_network", "public_api",
)  # fmt: skip
# Eighth Schedule languages (+ en) -- only ``en`` is seeded at launch.
NOTICE_LOCALES = (
    "en", "as", "bn", "brx", "doi", "gu", "hi", "kn", "ks", "kok", "mai", "ml", "mni", "mr", "ne", "or", "pa",
    "sa", "sat", "sd", "ta", "te", "ur",
)  # fmt: skip
AI_CONSENT_STATES = ("granted", "pending", "withdrawn", "not_required")
DSR_KINDS = ("access", "correction", "erasure", "consent_withdrawal")
DSR_CHANNELS = ("candidate_portal", "email", "phone", "letter", "other")
DSR_STATUSES = ("received", "in_progress", "completed", "refused")
# Epic 35 (A3): DSR subjects beyond candidates (internal intake only for the non-candidate kinds).
DSR_SUBJECT_KINDS = ("candidate", "contact", "client_user", "vendor_user")
RETENTION_RUN_KINDS = ("inactive_candidates", "class_expiry")
ERASURE_RUN_STATUSES = ("pending", "running", "completed", "failed")
RETENTION_RUN_STATUSES = ("previewed", "executing", "completed", "cancelled", "expired")
RETENTION_ITEM_STATUSES = ("pending", "erased", "skipped_now_active", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _candidate_fk(table: str, column: str = "candidate_id", ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column],
        ["candidates.tenant_id", "candidates.id"],
        name=f"fk_{table}_{column}_candidates",
        ondelete=ondelete,
    )


class ConsentNotice(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "consent_notices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "purpose", "locale", "version"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("purpose", CONSENT_PURPOSES), name="purpose"),
        CheckConstraint(_in("locale", NOTICE_LOCALES), name="locale"),
        Index(
            "uq_consent_notices_active",
            "tenant_id",
            "purpose",
            "locale",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'en'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class Consent(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "consents"
    __audit_entity_id__ = "candidate_id"
    __pii_subject__ = "candidate"
    __table_args__ = (
        _candidate_fk("consents"),
        CheckConstraint(_in("purpose", CONSENT_PURPOSES), name="purpose"),
        CheckConstraint(_in("status", CONSENT_STATUSES), name="status"),
        CheckConstraint(_in("source", CONSENT_SOURCES), name="source"),
        CheckConstraint("purpose <> 'client_submission' OR client_id IS NOT NULL", name="client_required"),
        Index("ix_consents_candidate_purpose", "candidate_id", "purpose"),
    )
    # Evidence (IP hash, user agent, attestation text) is personal data: not copied to the audit log.
    __audit_exclude__ = ("evidence",)

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    notice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_by_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class DsrRequest(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "dsr_requests"
    # Requester PII is encrypted in the audit log under the subject ``dsr_request:<id>``; erasure of the
    # candidate shreds the keys of all their DSRs too (``privacy.audit_shred`` handler).
    __pii_fields__ = ("requester_name", "requester_email")
    __table_args__ = (
        UniqueConstraint("tenant_id", "human_id"),
        UniqueConstraint("tenant_id", "id"),
        _candidate_fk("dsr_requests"),
        CheckConstraint(_in("kind", DSR_KINDS), name="kind"),
        CheckConstraint(_in("channel", DSR_CHANNELS), name="channel"),
        CheckConstraint(_in("status", DSR_STATUSES), name="status"),
        CheckConstraint(_in("subject_kind", DSR_SUBJECT_KINDS), name="subject_kind"),
        Index("ix_dsr_requests_status_due", "status", "due_at"),
        Index("ix_dsr_requests_candidate_id", "candidate_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    requester_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    requester_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'received'"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    identity_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    identity_verified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    due_soon_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    overdue_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 35 (migration 20261005_222_01): the data subject (``candidate`` rows keep ``candidate_id``).
    subject_kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'candidate'"))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class LegalHold(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "legal_holds"
    __table_args__ = (
        _candidate_fk("legal_holds"),
        Index("ix_legal_holds_candidate_id", "candidate_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    placed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ErasureRun(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "erasure_runs"
    __table_args__ = (
        _candidate_fk("erasure_runs"),
        CheckConstraint(_in("status", ERASURE_RUN_STATUSES), name="status"),
        CheckConstraint(_in("reason", ("dsr", "retention")), name="reason"),
        Index("ix_erasure_runs_candidate_id", "candidate_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    dsr_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    retention_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    handler_results: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Epic 35 (AD-34): ``[{class, table, count, retained_until, reason}]`` -- "what we kept and why".
    retained_summary: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RetentionPolicy(Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "retention_policies"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint(
            "candidate_inactive_months IS NULL OR candidate_inactive_months BETWEEN 6 AND 120",
            name="inactive_months",
        ),
        CheckConstraint("audit_retention_years BETWEEN 1 AND 10", name="audit_years"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    candidate_inactive_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_execute: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    audit_retention_years: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("7"))


class RetentionRun(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "retention_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", RETENTION_RUN_STATUSES), name="status"),
        CheckConstraint(_in("kind", RETENTION_RUN_KINDS), name="kind"),
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # Epic 35: ``inactive_candidates`` (P1 policy) or ``class_expiry`` (retention classes, AD-34).
    kind: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'inactive_candidates'"))
    criteria: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    executed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_execute_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RetentionRunItem(UUIDPk, TenantMixin, Base):
    __tablename__ = "retention_run_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["retention_runs.tenant_id", "retention_runs.id"],
            name="fk_retention_run_items_run_id_retention_runs",
            ondelete="CASCADE",
        ),
        _candidate_fk("retention_run_items"),
        UniqueConstraint("run_id", "candidate_id"),
        CheckConstraint(_in("status", RETENTION_ITEM_STATUSES), name="status"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'pending'"))


# ================================================================ retention classes (Epic 13, AD-34)
RETENTION_BASES = ("document_date", "adjudication", "end_of_engagement")
# Epic 35 (A3, migration 20261005_222_01): anchors and expiry actions.
RETENTION_ANCHORS = ("document_date", "fy_end", "adjudicated_at", "signed_at", "placement_end", "period_end")
RETENTION_EXPIRY_ACTIONS = ("anonymize", "delete", "report_only")


class RetentionClass(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """How long a class of records (financial documents, signed agreements, timesheets, BGV evidence) must be
    kept and when it may be deleted (migration 20261002_200_06; seeded per tenant). The UI and the expiry job
    are Epic 35, which may ALTER the table (anchor, expiry action, floors) without renaming these columns."""

    __tablename__ = "retention_classes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key"),
        CheckConstraint(_in("basis", RETENTION_BASES), name="basis"),
        CheckConstraint("min_retention_months >= 0", name="min_retention_months"),
        CheckConstraint(
            "delete_after_months IS NULL OR delete_after_months >= min_retention_months", name="delete_after"
        ),
        CheckConstraint(_in("anchor", RETENTION_ANCHORS), name="anchor"),
        CheckConstraint(_in("expiry_action", RETENTION_EXPIRY_ACTIONS), name="expiry_action"),
        CheckConstraint("min_retention_months >= floor_months", name="floor"),
    )

    key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    basis: Mapped[str] = mapped_column(String(20), nullable=False)
    min_retention_months: Mapped[int] = mapped_column(Integer, nullable=False)
    delete_after_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # Epic 35 (migration 20261005_222_01). The period is ``min_retention_months``; ``floor_months`` is the
    # statutory floor of a system class (never shortened below it, 422 ``below_statutory_floor``).
    anchor: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'document_date'"))
    expiry_action: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'anonymize'"))
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    floor_months: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    auto_execute: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RetentionOverride(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """A longer retention for one subject and class (e.g. a Contract requiring longer BGV retention; Epic 7 sets
    it). Expiry and erasure keep the class's rows of that subject until ``until_date``."""

    __tablename__ = "retention_overrides"
    __table_args__ = (
        UniqueConstraint("tenant_id", "subject_kind", "subject_id", "class_key", name="uq_retention_overrides_subject_class"),
        CheckConstraint(_in("subject_kind", DSR_SUBJECT_KINDS), name="subject_kind"),
        Index("ix_retention_overrides_class", "tenant_id", "class_key"),
    )

    subject_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    class_key: Mapped[str] = mapped_column(String(40), nullable=False)
    until_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
