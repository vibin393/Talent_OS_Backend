"""Interview tables (Epic 7: stage templates, per-demand plans, kits, interviews, participants, invite log,
scorecards, stage outcomes). All tenant-owned with forced RLS.

Tenant-internal references use composite foreign keys ``(tenant_id, x_id) -> x(tenant_id, id)`` because FK
checks bypass RLS. Times are ``timestamptz`` in UTC (AD-14); ``interviews.timezone`` is the organiser's zone
for display only.

Migrations: 20261001_70 (settings, kits, templates, stages), 20261001_71 (interviews, participants, invite
log, scorecards, stage outcomes). Free text that can hold personal data (notes, scorecard notes / summaries,
outcome comments, participant names / emails) is kept out of the audit log via ``__audit_exclude__``.
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
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

STAGE_KINDS = ("technical", "hr", "client", "managerial", "other", "assessment")  # Epic 36 (300_01): +assessment
INTERVIEW_MODES = ("video", "phone", "in_person")
INTERVIEW_STATUSES = ("scheduled", "completed", "cancelled", "no_show")
PROVIDERS = ("none", "google", "microsoft", "fake")
PARTICIPANT_KINDS = ("interviewer_internal", "interviewer_external", "candidate", "observer")
RESPONSE_STATUSES = ("needs_action", "accepted", "declined", "tentative")  # Epic 72 (D-18): + tentative
INVITE_METHODS = ("REQUEST", "CANCEL")
INVITE_CHANNELS = ("ics_email", "provider")
RECOMMENDATIONS = ("strong_yes", "yes", "no", "strong_no")
SCORECARD_STATUSES = ("draft", "submitted")
SCORECARD_SOURCES = ("in_app", "secure_link")
OUTCOMES = ("advance", "reject", "hold")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ settings / kits / templates / plans
class InterviewSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant, created lazily (``GET/PATCH /interviews/settings``)."""

    __tablename__ = "interview_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("scorecard_reminder_after_hours BETWEEN 1 AND 720", name="scorecard_reminder"),
        CheckConstraint("external_scorecard_link_days_after BETWEEN 1 AND 90", name="link_days"),
        CheckConstraint("default_duration_minutes BETWEEN 15 AND 480", name="duration"),
        CheckConstraint("rating_scale BETWEEN 3 AND 10", name="rating_scale"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    reminder_offsets_minutes: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[1440, 60]'::jsonb")
    )
    scorecard_reminder_after_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("24")
    )
    external_scorecard_link_days_after: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("7")
    )
    default_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("60"))
    rating_scale: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))


class InterviewKit(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "interview_kits"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("rating_scale BETWEEN 3 AND 10", name="rating_scale"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    competencies: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    questions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    rating_scale: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    recommendation_options: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text('\'["strong_yes", "yes", "no", "strong_no"]\'::jsonb')
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InterviewStageTemplate(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "interview_stage_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index(
            "uq_interview_stage_templates_default",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default AND archived_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    stages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InterviewStage(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """One stage of a demand's interview plan. Stages holding interviews are archived, never deleted."""

    __tablename__ = "interview_stages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("demand_id", "demands", "fk_interview_stages_demand_id_demands"),
        _tenant_fk("kit_id", "interview_kits", "fk_interview_stages_kit_id_interview_kits"),
        CheckConstraint(_in("kind", STAGE_KINDS), name="kind"),
        CheckConstraint("default_duration_minutes BETWEEN 15 AND 480", name="duration"),
        Index(
            "uq_interview_stages_demand_position",
            "demand_id",
            "position",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index("ix_interview_stages_demand_id", "demand_id"),
        # Epic 36 (300_03): assessment stages (stage-plan validation is Epic 40's).
        _tenant_fk("assessment_id", "assessments", "fk_interview_stages_assessment_id_assessments"),
        CheckConstraint("assessment_id IS NULL OR kind = 'assessment'", name="assessment_kind"),
        Index("ix_interview_stages_assessment_id", "assessment_id"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    kit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    default_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("60"))
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)  # Epic 36


# ================================================================ interviews
class Interview(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "interviews"
    __audit_exclude__ = ("notes", "kit_snapshot", "reminder_state", "meeting_url", "location")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        UniqueConstraint("ical_uid"),
        _tenant_fk("application_id", "applications", "fk_interviews_application_id_applications"),
        _tenant_fk("demand_id", "demands", "fk_interviews_demand_id_demands"),
        _tenant_fk("candidate_id", "candidates", "fk_interviews_candidate_id_candidates"),
        _tenant_fk("stage_id", "interview_stages", "fk_interviews_stage_id_interview_stages"),
        _tenant_fk("organizer_user_id", "users", "fk_interviews_organizer_user_id_users"),
        CheckConstraint(_in("mode", INTERVIEW_MODES), name="mode"),
        CheckConstraint(_in("status", INTERVIEW_STATUSES), name="status"),
        CheckConstraint(_in("provider", PROVIDERS), name="provider"),
        CheckConstraint("ends_at > starts_at", name="times"),
        Index("ix_interviews_application_id", "application_id"),
        Index("ix_interviews_demand_id_starts_at", "demand_id", "starts_at"),
        Index("ix_interviews_status_starts_at", "status", "starts_at"),
        Index("ix_interviews_candidate_id", "candidate_id"),
        Index("ix_interviews_stage_id", "stage_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stage_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stage_name: Mapped[str] = mapped_column(String(200), nullable=False)
    kit_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    round: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    meeting_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'scheduled'"))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ical_uid: Mapped[str] = mapped_column(String(200), nullable=False)
    organizer_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'none'"))
    provider_event_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    create_online_meeting: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    send_invites: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reminder_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class InterviewParticipant(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """Interviewers, observers and the candidate. Removed participants keep their row (``removed_at``) so
    the invite log and scorecards keep pointing at them."""

    __tablename__ = "interview_participants"
    __audit_exclude__ = ("email", "name")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("interview_id", "email"),
        _tenant_fk(
            "interview_id", "interviews", "fk_interview_participants_interview_id_interviews", "CASCADE"
        ),
        _tenant_fk("user_id", "users", "fk_interview_participants_user_id_users"),
        CheckConstraint(_in("kind", PARTICIPANT_KINDS), name="kind"),
        CheckConstraint(_in("response_status", RESPONSE_STATUSES), name="response_status"),
        Index("ix_interview_participants_user_id", "user_id"),
    )

    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'needs_action'")
    )
    scorecard_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InterviewInviteLog(UUIDPk, TenantMixin, Base):
    """Idempotency + trace of invitations: one row per (interview, sequence, participant, method)."""

    __tablename__ = "interview_invite_log"
    __table_args__ = (
        UniqueConstraint("interview_id", "sequence", "participant_id", "method"),
        _tenant_fk(
            "interview_id", "interviews", "fk_interview_invite_log_interview_id_interviews", "CASCADE"
        ),
        _tenant_fk(
            "participant_id",
            "interview_participants",
            "fk_interview_invite_log_participant_id_interview_participants",
            "CASCADE",
        ),
        CheckConstraint(_in("method", INVITE_METHODS), name="method"),
        CheckConstraint(_in("channel", INVITE_CHANNELS), name="channel"),
    )

    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    participant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Scorecard(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "scorecards"
    __audit_exclude__ = ("kit_snapshot", "ratings", "answers", "summary")
    __table_args__ = (
        UniqueConstraint("interview_id", "participant_id"),
        _tenant_fk("interview_id", "interviews", "fk_scorecards_interview_id_interviews", "CASCADE"),
        _tenant_fk(
            "participant_id", "interview_participants", "fk_scorecards_participant_id_interview_participants"
        ),
        CheckConstraint(_in("status", SCORECARD_STATUSES), name="status"),
        CheckConstraint(_in("source", SCORECARD_SOURCES), name="source"),
        CheckConstraint(
            f"recommendation IS NULL OR {_in('recommendation', RECOMMENDATIONS)}", name="recommendation"
        ),
    )

    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    participant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kit_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ratings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    answers: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    recommendation: Mapped[str | None] = mapped_column(String(20), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'in_app'"))
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class InterviewStageOutcome(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "interview_stage_outcomes"
    __audit_exclude__ = ("comment",)
    __table_args__ = (
        _tenant_fk(
            "application_id",
            "applications",
            "fk_interview_stage_outcomes_application_id_applications",
            "CASCADE",
        ),
        _tenant_fk("stage_id", "interview_stages", "fk_interview_stage_outcomes_stage_id_interview_stages"),
        CheckConstraint(_in("outcome", OUTCOMES), name="outcome"),
        Index("ix_interview_stage_outcomes_application_stage", "application_id", "stage_id"),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stage_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(10), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
