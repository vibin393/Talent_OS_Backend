# ruff: noqa: E501
"""Epic 62 (story 62.5, FR-310): Onboarding Q&A threads and messages.

* ``onboarding_qa_threads`` (audited header) -- a Worker's / pre-joining Candidate's onboarding questions.
* ``onboarding_qa_messages`` (not audited; ``text`` is candidate PII) -- questions, grounded answers with citations,
  hand-offs and coordinator replies.

Worker-visible: tenant RLS + worker org RLS on ``candidate_id``.

Revision ID: 20261008_409_02_onb_qa
Revises: 20261008_409_01_knowledge
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_409_02_onb_qa"
down_revision: str | tuple[str, ...] | None = "20261008_409_01_knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ("asker", "assistant", "coordinator", "system")
OUTCOMES = ("answered", "handed_off", "ungrounded", "pending_review", "blocked")
TOPICS = ("pay", "tax", "legal", "immigration", "documents", "policy", "it", "other")
FEEDBACK = ("helpful", "not_helpful", "incorrect")
TABLES = ("onboarding_qa_threads", "onboarding_qa_messages")


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


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Uuid(), nullable=nullable)


def upgrade() -> None:
    t = "onboarding_qa_threads"
    op.create_table(
        t,
        *_base(t),
        _uuid("candidate_id", False),
        _uuid("onboarding_case_id"),
        _uuid("placement_id"),
        sa.Column("audience", sa.String(10), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'open'"), nullable=False),
        _uuid("coordinator_user_id"),
        _uuid("consent_id"),
        _uuid("agent_run_id"),
        _uuid("task_id"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        _fk("candidate_id", "candidates", "fk_onboarding_qa_threads_candidate_id"),
        _fk("coordinator_user_id", "users", "fk_onboarding_qa_threads_coordinator"),
        _ck(t, "audience", "audience IN ('worker', 'candidate')"),
        _ck(t, "status", "status IN ('open', 'answered', 'handed_off', 'closed')"),
    )
    op.create_index("ix_onboarding_qa_threads_candidate_created", t, ["candidate_id", "created_at"])

    t = "onboarding_qa_messages"
    op.create_table(
        t,
        *_base(t),
        _uuid("thread_id", False),
        _uuid("candidate_id", False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(12), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("topic", sa.String(12), nullable=True),
        _uuid("agent_run_id"),
        _uuid("proposal_id"),
        _uuid("author_user_id"),
        sa.Column("feedback", sa.String(12), nullable=True),
        sa.Column("feedback_note", sa.String(500), nullable=True),
        sa.UniqueConstraint("tenant_id", "thread_id", "seq", name="uq_onboarding_qa_messages_seq"),
        _fk("thread_id", "onboarding_qa_threads", "fk_onboarding_qa_messages_thread_id", "CASCADE"),
        _ck(t, "role", _in("role", ROLES)),
        _ck(t, "outcome", f"outcome IS NULL OR {_in('outcome', OUTCOMES)}"),
        _ck(t, "topic", f"topic IS NULL OR {_in('topic', TOPICS)}"),
        _ck(t, "feedback", f"feedback IS NULL OR {_in('feedback', FEEDBACK)}"),
        _ck(t, "text_length", "char_length(text) <= 2000"),
    )
    op.create_index("ix_onboarding_qa_messages_candidate_id", t, ["candidate_id"])

    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table, worker_col="candidate_id")


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
