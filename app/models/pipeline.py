"""Pipeline tables (Epic 6: applications, stages, screenings, submissions, client feedback).

All tenant-owned with forced RLS. Tenant-internal references use composite foreign keys
``(tenant_id, x_id) -> x(tenant_id, id)`` because FK checks bypass RLS.

Migrations: 20261001_61 (settings, stages, reason codes, applications, history, questionnaires, screenings),
20261001_62 (submissions, candidate/client firsts, client feedback). Consents, notices and secure links are
Epic 12's tables (``app.models.privacy``, ``app.core.secure_links``) and are reused, not duplicated.

Free text that can hold personal data (screening answers / notes, email bodies, feedback comments) is kept
out of the audit log via ``__audit_exclude__``.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

STAGE_KINDS = ("anchored", "custom")
ANCHORED_STAGE_KEYS = (
    "applied",
    "shortlisted",
    "submitted",
    "interviewing",
    "selected",
    "offered",
    "hired",
    "rejected",
    "withdrawn",
)
APPLICATION_SOURCES = (
    "manual",
    "search",
    "match",
    "ai_match",
    "saved_list",
    "candidate_360",
    "career_portal",
    "import",
    "api",
    "vendor",  # Epic 17: accepted vendor submissions (migration 20261004_204_01; created by the vendors domain only)
    # Epic 36 (300_01): Phase 3 sources; the link lives on the owner's table + ``applications.source_ref``.
    "internal",
    "referral",
    "campaign",
    "campus",
    "event",
    # Epic 66 (500_01): Phase 5 sources (board applicants: 70; Job Network: 75; Public API: 67).
    "job_board",
    "job_network",
    "public_api",
)
REASON_KINDS = ("reject", "withdraw", "reopen")
SCREENING_KINDS = ("recruiter", "ai")
SCREENING_STATUSES = ("draft", "sent", "consent_declined", "answered", "reviewed", "completed", "expired")
SCREENING_OUTCOMES = ("proceed", "hold", "reject")
SUBMISSION_STATUSES = ("draft", "pending_approval", "approved", "sent", "cancelled", "approval_rejected")
RESUME_KINDS = ("original", "client_formatted")
FEEDBACK_STATUSES = ("none", "interested", "rejected_pending", "rejected", "on_hold")
FEEDBACK_DECISIONS = ("interested", "rejected", "on_hold")
FEEDBACK_SOURCES = ("link", "recorded")
CONSENT_MODES = ("candidate_link", "attestation_allowed")
RATE_UNITS = ("hour", "day", "month", "year")

MONEY = Numeric(14, 2)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ settings
class PipelineSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant, created lazily. Secure-link expiries live in ``settings_service.link_ttl``."""

    __tablename__ = "pipeline_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint(_in("submission_consent_mode", CONSENT_MODES), name="consent_mode"),
        CheckConstraint("prior_submission_window_days BETWEEN 0 AND 3650", name="prior_window"),
        CheckConstraint("min_margin_pct BETWEEN -100 AND 100", name="min_margin"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    submission_consent_mode: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'attestation_allowed'")
    )
    prior_submission_window_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("90")
    )
    min_margin_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("15"))
    burden_pct: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text(
            """'{"contract": 18, "contract_to_hire": 18, "permanent": 0, "freelance": 0}'::jsonb"""
        ),
    )
    rejection_email_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    rejection_email_subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    rejection_email_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_interview_on_interest: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    show_bill_rate_in_submission: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


# ================================================================ stages / reasons
class PipelineStage(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "pipeline_stages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("kind", STAGE_KINDS), name="kind"),
    )

    key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PipelineReasonCode(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "pipeline_reason_codes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "code"),
        CheckConstraint(_in("kind", REASON_KINDS), name="kind"),
    )

    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ applications
class Application(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "candidate_id", "demand_id"),
        UniqueConstraint("tenant_id", "human_id"),
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("candidate_id", "candidates", "fk_applications_candidate_id_candidates"),
        _tenant_fk("demand_id", "demands", "fk_applications_demand_id_demands"),
        _tenant_fk("stage_id", "pipeline_stages", "fk_applications_stage_id_pipeline_stages"),
        _tenant_fk("owner_id", "users", "fk_applications_owner_id_users"),
        CheckConstraint(_in("source", APPLICATION_SOURCES), name="source"),
        CheckConstraint(
            f"last_client_feedback IS NULL OR {_in('last_client_feedback', FEEDBACK_DECISIONS)}",
            name="last_feedback",
        ),
        Index("ix_applications_demand_id_stage_id", "demand_id", "stage_id"),
        Index("ix_applications_candidate_id", "candidate_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stage_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stage_entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    screening_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    last_client_feedback: Mapped[str | None] = mapped_column(String(20), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApplicationStageHistory(UUIDPk, TenantMixin, AuditedMixin, Base):
    __tablename__ = "application_stage_history"
    __audit_exclude__ = ("note",)
    __table_args__ = (
        _tenant_fk(
            "application_id", "applications", "fk_application_stage_history_application_id", "CASCADE"
        ),
        Index("ix_application_stage_history_application_id_at", "application_id", "at"),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_stage_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    to_stage_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ================================================================ screenings
class ScreeningQuestionnaire(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "screening_questionnaires"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index(
            "uq_screening_questionnaires_default",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default AND archived_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    questions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Screening(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "screenings"
    # Answers / notes / summaries are personal data: never copied into the audit log.
    __audit_exclude__ = ("questions", "answers", "notes", "ai_summary")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("application_id", "applications", "fk_screenings_application_id_applications", "CASCADE"),
        CheckConstraint(_in("kind", SCREENING_KINDS), name="kind"),
        CheckConstraint(_in("status", SCREENING_STATUSES), name="status"),
        CheckConstraint(f"outcome IS NULL OR {_in('outcome', SCREENING_OUTCOMES)}", name="outcome"),
        Index("ix_screenings_application_id", "application_id"),
        Index("ix_screenings_candidate_id", "candidate_id"),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    questionnaire_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    questions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(10), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    ai_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    questions_ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    summary_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    invite_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invite_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    completed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ submissions
class Submission(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "submissions"
    # Email text and the recruiter note may carry personal data: kept out of the audit log.
    __audit_exclude__ = ("email_subject", "email_body", "note", "email_message_ids")
    __table_args__ = (
        UniqueConstraint("tenant_id", "human_id"),
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("application_id", "applications", "fk_submissions_application_id_applications"),
        _tenant_fk("demand_id", "demands", "fk_submissions_demand_id_demands"),
        _tenant_fk("client_id", "clients", "fk_submissions_client_id_clients"),
        _tenant_fk("candidate_id", "candidates", "fk_submissions_candidate_id_candidates"),
        CheckConstraint(_in("status", SUBMISSION_STATUSES), name="status"),
        CheckConstraint(_in("resume_kind", RESUME_KINDS), name="resume_kind"),
        CheckConstraint(_in("feedback_status", FEEDBACK_STATUSES), name="feedback_status"),
        CheckConstraint(_in("rate_unit", RATE_UNITS), name="rate_unit"),
        Index("ix_submissions_application_id", "application_id"),
        Index("ix_submissions_demand_id_status", "demand_id", "status"),
        Index("ix_submissions_candidate_client_sent", "candidate_id", "client_id", "sent_at"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    pay_rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    bill_rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate_unit: Mapped[str] = mapped_column(String(10), nullable=False)
    employment_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    burden_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    margin_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    margin_pct: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False)
    below_min_margin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resume_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    resume_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resume_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipient_contact_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    cc_user_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    email_subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    email_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_message_ids: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    prior_submission_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    feedback_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'none'"))


class CandidateClientFirst(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """FR-24: who first submitted a candidate to a client (written on the first send)."""

    __tablename__ = "candidate_client_firsts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "client_id", "candidate_id"),
        _tenant_fk("client_id", "clients", "fk_candidate_client_firsts_client_id_clients"),
        _tenant_fk("candidate_id", "candidates", "fk_candidate_client_firsts_candidate_id_candidates"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    first_submitted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    first_submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ClientFeedback(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "client_feedback"
    __audit_exclude__ = ("comment",)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("submission_id", "submissions", "fk_client_feedback_submission_id_submissions", "CASCADE"),
        CheckConstraint(_in("source", FEEDBACK_SOURCES), name="source"),
        CheckConstraint(_in("decision", FEEDBACK_DECISIONS), name="decision"),
        Index("ix_client_feedback_submission_id", "submission_id"),
        Index("ix_client_feedback_application_id", "application_id"),
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
