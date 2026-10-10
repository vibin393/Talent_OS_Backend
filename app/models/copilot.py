# ruff: noqa: E501
"""Recruitment Copilot tables (Epic 54, AD-64; migrations ``20261008_401_01_copilot_convs`` / ``…_401_02_copilot_settings``).
Owner: ``copilot_store`` (the only Copilot code that runs SQL).

* ``copilot_conversations`` -- one private conversation of one user (``agent_key`` ``copilot`` or a Copilot mode such
  as ``analytics``); purged after the tenant retention (``expires_at``).
* ``copilot_messages`` -- user questions, assistant answers and draft events (``content`` JSONB, discriminated by role);
  ``candidate_ids`` feeds candidate erasure.
* ``copilot_feedback`` -- helpful / unhelpful ratings per run (survive conversation deletion; no conversation content).
* ``copilot_settings`` -- one row per tenant (retention, rate limit, open-conversation cap, feedback comments).

Content tables are **not** ``AuditedMixin`` (audit rows would copy private content); lifecycle actions are audited
explicitly without content. ``agent_runs.conversation_id`` has no FK (coordinator decision C-1). Every table is tenant
RLS + internal-only org RLS.
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
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CONVERSATION_STATUSES = ("open", "closed")
MESSAGE_ROLES = ("user", "assistant", "event")
RATINGS = ("helpful", "unhelpful")
DEFAULT_RETENTION_DAYS = 90
DEFAULT_QUESTIONS_PER_HOUR = 60
DEFAULT_MAX_OPEN_CONVERSATIONS = 50


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class CopilotConversation(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "copilot_conversations"
    __candidate_pii__ = (
        True  # titles / summaries / page context can name a candidate (erasure ``copilot.messages``)
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("user_id", "users", "fk_copilot_conversations_user_id_users"),
        CheckConstraint(_in("status", CONVERSATION_STATUSES), name="status"),
        Index("ix_copilot_conversations_user_last", "tenant_id", "user_id", "last_message_at"),
        Index("ix_copilot_conversations_expires_at", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_key: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("'copilot'"))
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    title_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'open'"))
    context_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CopilotMessage(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "copilot_messages"
    __candidate_pii__ = True  # questions and answers can name a candidate (erasure ``copilot.messages``)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "conversation_id", "seq", name="uq_copilot_messages_conversation_seq"),
        _tenant_fk(
            "conversation_id",
            "copilot_conversations",
            "fk_copilot_messages_conversation_id_copilot_conversations",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("role", MESSAGE_ROLES), name="role"),
        Index("ix_copilot_messages_candidate_ids", "candidate_ids", postgresql_using="gin"),
        Index("ix_copilot_messages_run_id", "run_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CopilotFeedback(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "copilot_feedback"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "run_id", "user_id", name="uq_copilot_feedback_run_user"),
        # ``conversation_id`` is a plain reference (no FK): ON DELETE SET NULL on a composite tenant FK would null
        # tenant_id too; ``copilot_store`` nulls it when a conversation is deleted, so feedback survives (FR-279).
        _tenant_fk("user_id", "users", "fk_copilot_feedback_user_id_users"),
        CheckConstraint(_in("rating", RATINGS), name="rating"),
        Index("ix_copilot_feedback_created_at", "tenant_id", "created_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_key: Mapped[str] = mapped_column(String(40), nullable=False)
    rating: Mapped[str] = mapped_column(String(10), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class CopilotSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created lazily with the defaults). Audited: holds no content."""

    __tablename__ = "copilot_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("retention_days BETWEEN 7 AND 365", name="retention_days"),
        CheckConstraint("questions_per_hour BETWEEN 5 AND 300", name="questions_per_hour"),
        CheckConstraint("max_open_conversations BETWEEN 5 AND 200", name="max_open_conversations"),
        _tenant_fk("updated_by", "users", "fk_copilot_settings_updated_by_users"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_RETENTION_DAYS))
    )
    questions_per_hour: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_QUESTIONS_PER_HOUR))
    )
    max_open_conversations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_MAX_OPEN_CONVERSATIONS))
    )
    allow_feedback_comments: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


__all__ = [
    "CONVERSATION_STATUSES",
    "DEFAULT_MAX_OPEN_CONVERSATIONS",
    "DEFAULT_QUESTIONS_PER_HOUR",
    "DEFAULT_RETENTION_DAYS",
    "MESSAGE_ROLES",
    "RATINGS",
    "CopilotConversation",
    "CopilotFeedback",
    "CopilotMessage",
    "CopilotSettings",
]
