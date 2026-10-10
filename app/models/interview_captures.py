# ruff: noqa: E501
"""Interview capture tables.

* ``interview_captures`` -- the hub (Epic 53 contract layer, AD-65; migration ``20261008_400_03_p4_hubs``), ALTERed by
  Epic 60 in ``20261008_407_01_ia_capture`` (requester / uploader, capture kind, confirmed location, blocked reasons,
  media type, recording deletion, attestation, failure code, detected speakers).
* Epic 60 (Interview Assistant, FR-301..FR-304): ``interview_capture_acks`` (interviewers' recording acknowledgements),
  ``interview_assistant_settings`` (one row per tenant), ``interview_capture_outputs`` (summary by competency and Q&A
  mapping), ``interview_scorecard_drafts`` (one private draft per interviewer; recommendation always empty) and
  ``scorecard_suggestion_decisions`` (accepted / changed / ignored suggested ratings, FR-323 selection data).

Free text (summaries, quotes, rationales) stays out of the audit log (``__audit_exclude__``).
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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CAPTURE_STATUSES = ("planned", "consent_pending", "blocked", "uploaded", "processing", "ready", "erased")
CAPTURE_KINDS = ("recording", "transcript", "both")
ACK_PARTICIPANT_KINDS = ("user", "external")
ACK_SCOPES = ("standing", "interview")
OUTPUT_STATUSES = ("ready", "superseded", "erased")
DRAFT_STATUSES = ("ready", "opened", "applied", "dismissed", "superseded")
DECISIONS = ("accepted", "changed", "ignored")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class InterviewCapture(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "interview_captures"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "interview_id"),
        _tenant_fk("interview_id", "interviews", "fk_interview_captures_interview_id_interviews"),
        _tenant_fk("candidate_id", "candidates", "fk_interview_captures_candidate_id_candidates"),
        _tenant_fk("file_id", "stored_files", "fk_interview_captures_file_id_stored_files"),
        _tenant_fk("transcript_id", "transcripts", "fk_interview_captures_transcript_id_transcripts"),
        _tenant_fk("agent_run_id", "agent_runs", "fk_interview_captures_agent_run_id_agent_runs"),
        _tenant_fk("requested_by", "users", "fk_interview_captures_requested_by_users"),
        _tenant_fk("uploaded_by", "users", "fk_interview_captures_uploaded_by_users"),
        CheckConstraint(_in("status", CAPTURE_STATUSES), name="status"),
        CheckConstraint("file_id IS NULL OR recording_allowed", name="recording_allowed"),
        CheckConstraint(f"capture_kind IS NULL OR {_in('capture_kind', CAPTURE_KINDS)}", name="capture_kind"),
    )

    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'planned'"))
    recording_consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    participant_acks: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    jurisdiction: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recording_rule: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recording_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Epic 60 (407_01)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    capture_kind: Mapped[str | None] = mapped_column(String(12), nullable=True)
    confirmed_country: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)
    confirmed_region: Mapped[str | None] = mapped_column(String(10), nullable=True)
    blocked_reasons: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    file_media_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    recording_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    speakers: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))


class InterviewCaptureAck(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """An interviewer's recording acknowledgement: internal Users once per tenant (``standing``, revocable), external
    (Client) interviewers per Interview (``interview``, through a Secure Link)."""

    __tablename__ = "interview_capture_acks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("user_id", "users", "fk_interview_capture_acks_user_id_users"),
        _tenant_fk(
            "interview_id", "interviews", "fk_interview_capture_acks_interview_id_interviews", "CASCADE"
        ),
        _tenant_fk(
            "interview_participant_id",
            "interview_participants",
            "fk_interview_capture_acks_participant_id",
            "CASCADE",
        ),
        CheckConstraint(_in("participant_kind", ACK_PARTICIPANT_KINDS), name="participant_kind"),
        CheckConstraint(_in("scope", ACK_SCOPES), name="scope"),
        CheckConstraint("(participant_kind = 'user') = (user_id IS NOT NULL)", name="user_kind"),
        CheckConstraint(
            "scope <> 'standing' OR (participant_kind = 'user' AND interview_id IS NULL)",
            name="standing_scope",
        ),
        CheckConstraint("scope <> 'interview' OR interview_id IS NOT NULL", name="interview_scope"),
        Index(
            "uq_interview_capture_acks_standing",
            "tenant_id",
            "user_id",
            unique=True,
            postgresql_where=text("scope = 'standing' AND revoked_at IS NULL"),
        ),
        Index(
            "uq_interview_capture_acks_participant",
            "tenant_id",
            "interview_participant_id",
            unique=True,
            postgresql_where=text("scope = 'interview' AND revoked_at IS NULL"),
        ),
        Index("ix_interview_capture_acks_interview_id", "interview_id"),
    )

    participant_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    interview_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    interview_participant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    acked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    statement_version: Mapped[str] = mapped_column(String(20), nullable=False)


class InterviewAssistantSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created lazily with the defaults)."""

    __tablename__ = "interview_assistant_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("consent_link_days BETWEEN 1 AND 30", name="consent_link_days"),
        CheckConstraint("max_upload_mb BETWEEN 1 AND 200", name="max_upload_mb"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    show_suggestions_by_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    allow_recording_upload: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    allow_transcript_upload: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    consent_link_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("7"))
    max_upload_mb: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("200"))


class InterviewCaptureOutput(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "interview_capture_outputs"
    __audit_exclude__ = ("summary_by_competency", "qa_items", "overall_summary")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("capture_id", "interview_captures", "fk_interview_capture_outputs_capture_id", "CASCADE"),
        CheckConstraint(_in("status", OUTPUT_STATUSES), name="status"),
        CheckConstraint(
            "overall_summary IS NULL OR char_length(overall_summary) <= 2000", name="overall_summary_length"
        ),
        Index(
            "uq_interview_capture_outputs_ready",
            "tenant_id",
            "capture_id",
            unique=True,
            postgresql_where=text("status = 'ready'"),
        ),
        Index("ix_interview_capture_outputs_capture_id", "capture_id"),
        Index("ix_interview_capture_outputs_candidate_id", "candidate_id"),
    )

    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    transcript_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    summary_by_competency: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    qa_items: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    overall_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'ready'"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class InterviewScorecardDraft(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """The assistant's private draft for one interviewer. ``recommendation`` is always NULL (FR-304, DB CHECK)."""

    __tablename__ = "interview_scorecard_drafts"
    __audit_exclude__ = ("ratings", "answers")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id", "capture_id", "participant_id", name="uq_interview_scorecard_drafts_participant"
        ),
        _tenant_fk("capture_id", "interview_captures", "fk_interview_scorecard_drafts_capture_id", "CASCADE"),
        _tenant_fk(
            "participant_id",
            "interview_participants",
            "fk_interview_scorecard_drafts_participant_id",
            "CASCADE",
        ),
        _tenant_fk("user_id", "users", "fk_interview_scorecard_drafts_user_id_users"),
        CheckConstraint("recommendation IS NULL", name="recommendation_empty"),
        CheckConstraint(_in("status", DRAFT_STATUSES), name="status"),
        Index("ix_interview_scorecard_drafts_interview_id", "interview_id"),
        Index("ix_interview_scorecard_drafts_user_id", "user_id"),
    )

    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    participant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    ratings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    answers: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    recommendation: Mapped[str | None] = mapped_column(String(12), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'ready'"))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ScorecardSuggestionDecision(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "scorecard_suggestion_decisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id", "draft_id", "competency_key", name="uq_scorecard_suggestion_decisions_competency"
        ),
        _tenant_fk(
            "draft_id", "interview_scorecard_drafts", "fk_scorecard_suggestion_decisions_draft_id", "CASCADE"
        ),
        CheckConstraint(_in("decision", DECISIONS), name="decision"),
    )

    draft_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    competency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    suggested_rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    final_rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "ACK_PARTICIPANT_KINDS",
    "ACK_SCOPES",
    "CAPTURE_KINDS",
    "CAPTURE_STATUSES",
    "DECISIONS",
    "DRAFT_STATUSES",
    "OUTPUT_STATUSES",
    "InterviewAssistantSettings",
    "InterviewCapture",
    "InterviewCaptureAck",
    "InterviewCaptureOutput",
    "InterviewScorecardDraft",
    "ScorecardSuggestionDecision",
]
