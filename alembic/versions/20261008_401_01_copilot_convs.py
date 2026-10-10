# ruff: noqa: E501
"""Epic 54 story 54.1 (FR-276/FR-279, AD-64): Copilot conversations, messages and feedback.

* ``copilot_conversations`` -- private per user; ``expires_at`` drives the purge job.
* ``copilot_messages`` -- ``content`` JSONB by role; GIN ``candidate_ids`` for erasure.
* ``copilot_feedback`` -- ratings per run; ``conversation_id`` is a plain reference (survives deletion).

No FK on ``agent_runs.conversation_id`` (coordinator decision C-1: Copilot and Screening join by id + agent_key).
Every table: tenant RLS + internal-only org RLS.

Revision ID: 20261008_401_01_copilot_convs
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_401_01_copilot_convs"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_copilot",)
depends_on: str | Sequence[str] | None = None

TABLES = ("copilot_conversations", "copilot_messages", "copilot_feedback")


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


def _fk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_{table}_{column}_{target}",
        ondelete=ondelete,
    )


def upgrade() -> None:
    t = "copilot_conversations"
    op.create_table(
        t,
        *_base(t),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_key", sa.String(40), server_default=sa.text("'copilot'"), nullable=False),
        sa.Column("title", sa.String(120), nullable=True),
        sa.Column("title_locked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'open'"), nullable=False),
        sa.Column("context_ref", postgresql.JSONB(), nullable=True),
        sa.Column("context_summary", sa.Text(), nullable=True),
        sa.Column("message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        _fk(t, "user_id", "users"),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_copilot_conversations_status")),
    )
    op.create_index("ix_copilot_conversations_user_last", t, ["tenant_id", "user_id", "last_message_at"])
    op.create_index("ix_copilot_conversations_expires_at", t, ["expires_at"])

    t = "copilot_messages"
    op.create_table(
        t,
        *_base(t),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "candidate_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "conversation_id", "seq", name="uq_copilot_messages_conversation_seq"
        ),
        _fk(t, "conversation_id", "copilot_conversations", ondelete="CASCADE"),
        sa.CheckConstraint("role IN ('user', 'assistant', 'event')", name=op.f("ck_copilot_messages_role")),
    )
    op.create_index("ix_copilot_messages_candidate_ids", t, ["candidate_ids"], postgresql_using="gin")
    op.create_index("ix_copilot_messages_run_id", t, ["run_id"])

    t = "copilot_feedback"
    op.create_table(
        t,
        *_base(t),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("rating", sa.String(10), nullable=False),
        sa.Column("comment", sa.String(1000), nullable=True),
        sa.UniqueConstraint("tenant_id", "run_id", "user_id", name="uq_copilot_feedback_run_user"),
        _fk(t, "user_id", "users"),
        sa.CheckConstraint("rating IN ('helpful', 'unhelpful')", name=op.f("ck_copilot_feedback_rating")),
    )
    op.create_index("ix_copilot_feedback_created_at", t, ["tenant_id", "created_at"])

    for table in TABLES:
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
    op.drop_table("copilot_feedback")
    op.drop_table("copilot_messages")
    op.drop_table("copilot_conversations")
