# ruff: noqa: E501
"""Analytics answers (Epic 64, FR-316..FR-318; branch ``p4_analytics_agent``, band 411).

``analytics_answers`` -- one row per ``semantic.run`` tool call of the Analytics agent (or the Copilot ``analytics``
capability): the model's restatement of the question, its stated assumptions, the validated foundation ``Definition``
(``definition`` JSONB, stored as-is), the report ``viz``, the deterministic explanation and run metadata (row count,
masked field keys, limit kind, suppressed cells). **Result rows are never stored**: the UI re-runs the definition as the
current viewer (``POST /analytics/answers/{id}/preview``).

* Private to its user (owner-only service checks + tenant RLS + internal-only org RLS). Not ``AuditedMixin``: the content
  is private; actions are audited explicitly (``analytics.answer_run`` / ``_previewed`` / ``_saved``).
* ``conversation_id`` / ``run_id`` are plain UUIDs (Copilot conversations and agent runs belong to other epics; no FK).
* ``saved_report_id`` -> ``report_definitions`` (P3) once the user saved it in the report builder (``ON DELETE SET NULL``).
* ``question`` / ``assumptions`` may hold names typed by the user: purged after 90 days (``expires_at``), erasure handler
  ``analytics_answers.records`` (``__candidate_pii__``).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, Timestamps, UUIDPk

LIMIT_KINDS = ("row_cap", "timeout")
ANSWER_STATUSES = ("ok", "needs_repair")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AnalyticsAnswer(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "analytics_answers"
    __candidate_pii__ = (
        True  # question / assumptions may name a candidate (erasure handler analytics_answers.records)
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_analytics_answers_user_id_users",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["saved_report_id"],
            ["report_definitions.id"],
            name="fk_analytics_answers_saved_report_id_report_definitions",
            ondelete="SET NULL",
        ),
        CheckConstraint(f"limit_kind IS NULL OR {_in('limit_kind', LIMIT_KINDS)}", name="limit_kind"),
        CheckConstraint(_in("status", ANSWER_STATUSES), name="status"),
        Index("ix_analytics_answers_user_created", "tenant_id", "user_id", text("created_at DESC")),
        Index("ix_analytics_answers_conversation_id", "conversation_id"),
        Index("ix_analytics_answers_expires_at", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    assumptions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    source_key: Mapped[str] = mapped_column(String(80), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    definition_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    viz: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    masked_fields: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    limit_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    suppressed_cells: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'ok'"))
    saved_report_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["ANSWER_STATUSES", "LIMIT_KINDS", "AnalyticsAnswer"]
