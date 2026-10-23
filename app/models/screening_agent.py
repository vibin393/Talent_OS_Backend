# ruff: file-ignore[line-too-long]
"""Screening Agent (Epic 57, FR-289..FR-292; migrations ``20261008_404_01_screen_qnrs`` / ``20261008_404_02_screen_convos``).
Owner: ``screening_agent``.

* ``screening_questionnaire_versions`` (audited) -- versioned, approved Agent Questions built on a P1
  ``screening_questionnaires`` template (no ALTER of the P1 table). One ``approved`` version per questionnaire.
* ``screening_questionnaire_bindings`` (audited) -- which questionnaire a tenant / client / demand uses.
* ``screening_agent_settings`` (audited) -- one row per tenant.
* ``screening_conversations`` (audited header) -- one multi-turn text pre-screening of an Application.
* ``screening_turns`` (not audited) -- the messages; candidate-visible (worker org RLS on ``candidate_id``).
* ``screening_answers`` (audited, values excluded) -- extracted answers for recruiter review (text and voice, Epic 58).

``agent_runs.conversation_id`` has no FK (coordinator decision C-1): conversations join runs by id + ``agent_key``.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

VERSION_STATUSES = ("draft", "approved", "retired")
BINDING_SCOPES = ("tenant", "client", "demand")
CHANNELS = ("portal_chat", "link_chat", "email", "whatsapp")
CONVERSATION_STATUSES = (
    "invited", "consented", "in_progress", "paused", "completed", "reviewed", "expired", "withdrawn", "handed_off",
    "taken_over", "consent_refused", "cancelled",
)  # fmt: skip
OPEN_STATUSES = ("invited", "consented", "in_progress", "paused")
HANDOFF_REASONS = (
    "candidate_request", "compensation", "ungrounded_question", "language", "model_failure", "alternative_process", "other",
)  # fmt: skip
TURN_DIRECTIONS = ("inbound", "outbound")
TURN_AUTHORS = ("agent", "candidate", "recruiter", "system")
TURN_KINDS = (
    "disclosure", "question", "clarification", "grounded_answer", "reminder", "handoff", "acknowledgement", "answer",
    "recruiter_message", "closing",
)  # fmt: skip
ANSWER_SOURCES = ("text", "voice")
ANSWER_STATUSES = ("unconfirmed", "accepted", "edited", "rejected")
ANSWER_FLAGS = ("knockout", "low_confidence", "invalid_value")
DEFAULT_CHANNELS = ("auto", "portal_chat", "link_chat", "email", "whatsapp")
MAX_TURN_BODY = 4000
MAX_ANSWER_TEXT = 2000


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _uuid_array() -> Any:
    return mapped_column(ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]"))


# ================================================================ 404_01 questionnaires
class ScreeningQuestionnaireVersion(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "screening_questionnaire_versions"
    __audit_exclude__ = ("questions", "clarifications")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "questionnaire_id", "version", name="uq_sqv_questionnaire_version"),
        _tenant_fk("questionnaire_id", "screening_questionnaires", "fk_sqv_questionnaire_id"),
        _tenant_fk("approved_by", "users", "fk_sqv_approved_by_users"),
        CheckConstraint(_in("status", VERSION_STATUSES), name="status"),
        CheckConstraint("status <> 'approved' OR approved_by IS NOT NULL", name="approved_by"),
        CheckConstraint("version >= 1", name="version"),
        Index(
            "uq_sqv_approved",
            "tenant_id",
            "questionnaire_id",
            unique=True,
            postgresql_where=text("status = 'approved'"),
        ),
    )

    questionnaire_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    questions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    clarifications: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'draft'"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    change_note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class ScreeningQuestionnaireBinding(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "screening_questionnaire_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("questionnaire_id", "screening_questionnaires", "fk_sqb_questionnaire_id"),
        CheckConstraint(_in("scope_kind", BINDING_SCOPES), name="scope_kind"),
        CheckConstraint("(scope_kind = 'tenant') = (scope_id IS NULL)", name="scope_id"),
    )

    questionnaire_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ScreeningAgentSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "screening_agent_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", name="uq_screening_agent_settings_tenant_id"),
        CheckConstraint("expiry_days BETWEEN 1 AND 14", name="expiry_days"),
        CheckConstraint("reminder_after_hours BETWEEN 4 AND 72", name="reminder_after_hours"),
        CheckConstraint("max_reminders BETWEEN 0 AND 2", name="max_reminders"),
        CheckConstraint("questions_per_turn BETWEEN 1 AND 3", name="questions_per_turn"),
        CheckConstraint("low_confidence_threshold BETWEEN 0.5 AND 0.95", name="low_confidence_threshold"),
        CheckConstraint(_in("default_channel", DEFAULT_CHANNELS), name="default_channel"),
    )

    expiry_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("7"))
    reminder_after_hours: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("24"))
    max_reminders: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("2"))
    questions_per_turn: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("2"))
    low_confidence_threshold: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, server_default=text("0.70")
    )
    stop_words: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    whatsapp_invite_template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    default_channel: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'auto'"))
    allow_campus_candidates: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# ================================================================ 404_02 conversations
class ScreeningConversation(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "screening_conversations"
    __audit_exclude__ = ("summary", "gaps", "flags")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id", name="uq_screening_conversations_human_id"),
        _tenant_fk("application_id", "applications", "fk_screening_conv_application_id"),
        _tenant_fk("candidate_id", "candidates", "fk_screening_conv_candidate_id"),
        _tenant_fk("demand_id", "demands", "fk_screening_conv_demand_id"),
        _tenant_fk("screening_id", "screenings", "fk_screening_conv_screening_id"),
        _tenant_fk(
            "questionnaire_version_id", "screening_questionnaire_versions", "fk_screening_conv_version_id"
        ),
        _tenant_fk("transcript_id", "transcripts", "fk_screening_conv_transcript_id"),
        _tenant_fk("owner_user_id", "users", "fk_screening_conv_owner_user_id"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        CheckConstraint(_in("status", CONVERSATION_STATUSES), name="status"),
        CheckConstraint(
            f"handoff_reason IS NULL OR {_in('handoff_reason', HANDOFF_REASONS)}", name="handoff_reason"
        ),
        CheckConstraint("reminders_sent BETWEEN 0 AND 5", name="reminders_sent"),
        CheckConstraint("summary IS NULL OR char_length(summary) <= 3000", name="summary_length"),
        Index(
            "uq_screening_conv_open",
            "tenant_id",
            "application_id",
            unique=True,
            postgresql_where=text("status IN ('invited', 'consented', 'in_progress', 'paused')"),
        ),
        Index("ix_screening_conversations_candidate_id", "candidate_id"),
        Index("ix_screening_conversations_status_expires", "status", "expires_at"),
        Index("ix_screening_conversations_application_id", "application_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    screening_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    questionnaire_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'invited'"))
    current_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    started_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    started_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    wa_window_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminders_sent: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    paused_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    taken_over_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    handoff_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    flags: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    gaps: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    suggested_next: Mapped[str | None] = mapped_column(String(10), nullable=True)
    non_english_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScreeningTurn(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "screening_turns"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "conversation_id", "seq", name="uq_screening_turns_conversation_seq"),
        _tenant_fk(
            "conversation_id", "screening_conversations", "fk_screening_turns_conversation_id", "CASCADE"
        ),
        CheckConstraint(_in("direction", TURN_DIRECTIONS), name="direction"),
        CheckConstraint(_in("author", TURN_AUTHORS), name="author"),
        CheckConstraint(_in("kind", TURN_KINDS), name="kind"),
        CheckConstraint(f"char_length(body) <= {MAX_TURN_BODY}", name="body_length"),
        Index("ix_screening_turns_candidate_id", "candidate_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    author: Mapped[str] = mapped_column(String(10), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    question_ids: Mapped[list[uuid.UUID]] = _uuid_array()
    citations: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    template_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    channel_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    transcript_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ScreeningAnswer(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "screening_answers"
    __audit_exclude__ = ("answer_text", "normalized", "edited_value")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(
            "conversation_id", "screening_conversations", "fk_screening_answers_conversation_id", "CASCADE"
        ),
        _tenant_fk(
            "questionnaire_version_id", "screening_questionnaire_versions", "fk_screening_answers_version_id"
        ),
        CheckConstraint(_in("source", ANSWER_SOURCES), name="source"),
        CheckConstraint(_in("status", ANSWER_STATUSES), name="status"),
        CheckConstraint("(source = 'text') = (conversation_id IS NOT NULL)", name="source_link"),
        CheckConstraint("status <> 'edited' OR edited_value IS NOT NULL", name="edited_value"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence"),
        CheckConstraint(
            f"answer_text IS NULL OR char_length(answer_text) <= {MAX_ANSWER_TEXT}", name="answer_length"
        ),
        Index(
            "uq_screening_answers_current",
            "tenant_id",
            "transcript_id",
            "question_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        Index("ix_screening_answers_conversation_id", "conversation_id"),
        Index("ix_screening_answers_voice_session_id", "voice_session_id"),
        Index("ix_screening_answers_candidate_id", "candidate_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(5), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    voice_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    transcript_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    questionnaire_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    question_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    segment_refs: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{}'::integer[]")
    )
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flags: Mapped[list[str]] = mapped_column(
        ARRAY(String(30)), nullable=False, server_default=text("'{}'::varchar[]")
    )
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'unconfirmed'"))
    edited_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


__all__ = [
    "ANSWER_FLAGS",
    "CHANNELS",
    "CONVERSATION_STATUSES",
    "HANDOFF_REASONS",
    "OPEN_STATUSES",
    "TURN_KINDS",
    "ScreeningAgentSettings",
    "ScreeningAnswer",
    "ScreeningConversation",
    "ScreeningQuestionnaireBinding",
    "ScreeningQuestionnaireVersion",
    "ScreeningTurn",
]
