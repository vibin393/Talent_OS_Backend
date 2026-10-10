# ruff: noqa: E501
"""Epic 57 (Screening Agent, stories 57.2-57.5): conversations, turns and extracted answers.

* ``screening_conversations`` (audited header; candidate-visible) -- one open conversation per Application
  (partial unique ``uq_screening_conv_open``). ``current_run_id`` is a plain UUID (``agent_runs``; coordinator C-1:
  no FK on ``agent_runs.conversation_id`` either).
* ``screening_turns`` (not audited; candidate-visible) -- messages; ``seq`` unique per conversation.
* ``screening_answers`` (audited, values excluded; internal only) -- text answers (``conversation_id``) and voice
  answers (``voice_session_id``, Epic 58, plain UUID).

Candidate-visible tables: tenant RLS + worker org RLS on ``candidate_id``; answers: internal org RLS.

Revision ID: 20261008_404_02_screen_convos
Revises: 20261008_404_01_screen_qnrs
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_404_02_screen_convos"
down_revision: str | tuple[str, ...] | None = "20261008_404_01_screen_qnrs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = (
    "invited", "consented", "in_progress", "paused", "completed", "reviewed", "expired", "withdrawn", "handed_off",
    "taken_over", "consent_refused", "cancelled",
)  # fmt: skip
HANDOFF = (
    "candidate_request", "compensation", "ungrounded_question", "language", "model_failure", "alternative_process", "other",
)  # fmt: skip
KINDS = (
    "disclosure", "question", "clarification", "grounded_answer", "reminder", "handoff", "acknowledgement", "answer",
    "recruiter_message", "closing",
)  # fmt: skip


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Uuid(), nullable=nullable)


def upgrade() -> None:
    t = "screening_conversations"
    op.create_table(
        t,
        *_base(t),
        sa.Column("human_id", sa.String(20), nullable=False),
        _uuid("application_id", False),
        _uuid("candidate_id", False),
        _uuid("demand_id", False),
        _uuid("screening_id"),
        _uuid("questionnaire_version_id", False),
        sa.Column("channel", sa.String(12), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'invited'"), nullable=False),
        _uuid("current_run_id"),
        _uuid("transcript_id"),
        _uuid("secure_link_id"),
        _uuid("consent_id"),
        _uuid("owner_user_id", False),
        sa.Column("started_by_kind", sa.String(20), nullable=False),
        _uuid("started_by_id"),
        _ts("invited_at"),
        _ts("consented_at"),
        _ts("completed_at"),
        _ts("expires_at", False),
        _ts("last_inbound_at"),
        _ts("last_outbound_at"),
        _ts("wa_window_until"),
        sa.Column("reminders_sent", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        _uuid("paused_by"),
        _uuid("taken_over_by"),
        sa.Column("handoff_reason", sa.String(40), nullable=True),
        sa.Column("flags", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("gaps", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("suggested_next", sa.String(10), nullable=True),
        sa.Column("non_english_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        _uuid("reviewed_by"),
        _ts("reviewed_at"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_screening_conversations_human_id"),
        _fk("application_id", "applications", "fk_screening_conv_application_id"),
        _fk("candidate_id", "candidates", "fk_screening_conv_candidate_id"),
        _fk("demand_id", "demands", "fk_screening_conv_demand_id"),
        _fk("screening_id", "screenings", "fk_screening_conv_screening_id"),
        _fk("questionnaire_version_id", "screening_questionnaire_versions", "fk_screening_conv_version_id"),
        _fk("transcript_id", "transcripts", "fk_screening_conv_transcript_id"),
        _fk("owner_user_id", "users", "fk_screening_conv_owner_user_id"),
        _ck(t, "channel", "channel IN ('portal_chat', 'link_chat', 'email', 'whatsapp')"),
        _ck(t, "status", _in("status", STATUSES)),
        _ck(t, "handoff_reason", f"handoff_reason IS NULL OR {_in('handoff_reason', HANDOFF)}"),
        _ck(t, "reminders_sent", "reminders_sent BETWEEN 0 AND 5"),
        _ck(t, "summary_length", "summary IS NULL OR char_length(summary) <= 3000"),
    )
    op.create_index(
        "uq_screening_conv_open",
        t,
        ["tenant_id", "application_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('invited', 'consented', 'in_progress', 'paused')"),
    )
    op.create_index("ix_screening_conversations_candidate_id", t, ["candidate_id"])
    op.create_index("ix_screening_conversations_status_expires", t, ["status", "expires_at"])
    op.create_index("ix_screening_conversations_application_id", t, ["application_id"])

    t = "screening_turns"
    op.create_table(
        t,
        *_base(t),
        _uuid("conversation_id", False),
        _uuid("candidate_id", False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("author", sa.String(10), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "question_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("citations", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("template_key", sa.String(60), nullable=True),
        _uuid("channel_message_id"),
        _uuid("email_message_id"),
        sa.Column("transcript_seq", sa.Integer(), nullable=True),
        _uuid("run_id"),
        _uuid("author_user_id"),
        sa.UniqueConstraint(
            "tenant_id", "conversation_id", "seq", name="uq_screening_turns_conversation_seq"
        ),
        _fk("conversation_id", "screening_conversations", "fk_screening_turns_conversation_id", "CASCADE"),
        _ck(t, "direction", "direction IN ('inbound', 'outbound')"),
        _ck(t, "author", "author IN ('agent', 'candidate', 'recruiter', 'system')"),
        _ck(t, "kind", _in("kind", KINDS)),
        _ck(t, "body_length", "char_length(body) <= 4000"),
    )
    op.create_index("ix_screening_turns_candidate_id", t, ["candidate_id"])

    t = "screening_answers"
    op.create_table(
        t,
        *_base(t),
        _uuid("candidate_id", False),
        _uuid("application_id", False),
        sa.Column("source", sa.String(5), nullable=False),
        _uuid("conversation_id"),
        _uuid("voice_session_id"),
        _uuid("transcript_id", False),
        _uuid("questionnaire_version_id", False),
        _uuid("question_id", False),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("normalized", postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column(
            "segment_refs",
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'::integer[]"),
            nullable=False,
        ),
        sa.Column("start_ms", sa.Integer(), nullable=True),
        sa.Column(
            "flags",
            postgresql.ARRAY(sa.String(30)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("status", sa.String(12), server_default=sa.text("'unconfirmed'"), nullable=False),
        sa.Column("edited_value", postgresql.JSONB(), nullable=True),
        _uuid("reviewed_by"),
        _ts("reviewed_at"),
        _ts("superseded_at"),
        _uuid("ai_action_id"),
        _fk("conversation_id", "screening_conversations", "fk_screening_answers_conversation_id", "CASCADE"),
        _fk(
            "questionnaire_version_id", "screening_questionnaire_versions", "fk_screening_answers_version_id"
        ),
        _ck(t, "source", "source IN ('text', 'voice')"),
        _ck(t, "status", "status IN ('unconfirmed', 'accepted', 'edited', 'rejected')"),
        _ck(t, "source_link", "(source = 'text') = (conversation_id IS NOT NULL)"),
        _ck(t, "edited_value", "status <> 'edited' OR edited_value IS NOT NULL"),
        _ck(t, "confidence", "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"),
        _ck(t, "answer_length", "answer_text IS NULL OR char_length(answer_text) <= 2000"),
    )
    op.create_index(
        "uq_screening_answers_current",
        t,
        ["tenant_id", "transcript_id", "question_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index("ix_screening_answers_conversation_id", t, ["conversation_id"])
    op.create_index("ix_screening_answers_voice_session_id", t, ["voice_session_id"])
    op.create_index("ix_screening_answers_candidate_id", t, ["candidate_id"])

    for table in ("screening_conversations", "screening_turns", "screening_answers"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
    enable_org_rls("screening_conversations", worker_col="candidate_id")
    enable_org_rls("screening_turns", worker_col="candidate_id")
    enable_org_rls("screening_answers")


def downgrade() -> None:
    for table in ("screening_answers", "screening_turns", "screening_conversations"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
