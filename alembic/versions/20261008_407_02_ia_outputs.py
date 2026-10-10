# ruff: noqa: E501
"""Interview Assistant (Epic 60, stories 60.3 / 60.5): ``interview_capture_outputs`` (summary by competency, Q&A
mapping), ``interview_scorecard_drafts`` (one private draft per interviewer; ``recommendation IS NULL`` -- FR-304) and
``scorecard_suggestion_decisions`` (accepted / changed / ignored suggested ratings).

Every table: tenant RLS + internal-only org RLS.

Revision ID: 20261008_407_02_ia_outputs
Revises: 20261008_407_01_ia_capture
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_407_02_ia_outputs"
down_revision: str | tuple[str, ...] | None = "20261008_407_01_ia_capture"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("interview_capture_outputs", "interview_scorecard_drafts", "scorecard_suggestion_decisions")


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


def upgrade() -> None:
    t = "interview_capture_outputs"
    op.create_table(
        t,
        *_base(t),
        sa.Column("capture_id", sa.Uuid(), nullable=False),
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("transcript_id", sa.Uuid(), nullable=False),
        sa.Column("summary_by_competency", postgresql.JSONB(), nullable=False),
        sa.Column("qa_items", postgresql.JSONB(), nullable=False),
        sa.Column("overall_summary", sa.Text(), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'ready'"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("ai_generated", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _fk("capture_id", "interview_captures", "fk_interview_capture_outputs_capture_id", "CASCADE"),
        _ck(t, "status", "status IN ('ready', 'superseded', 'erased')"),
        _ck(t, "overall_summary_length", "overall_summary IS NULL OR char_length(overall_summary) <= 2000"),
    )
    op.create_index("ix_interview_capture_outputs_tenant_id", t, ["tenant_id"])
    op.create_index("ix_interview_capture_outputs_capture_id", t, ["capture_id"])
    op.create_index("ix_interview_capture_outputs_candidate_id", t, ["candidate_id"])
    op.create_index(
        "uq_interview_capture_outputs_ready", t, ["tenant_id", "capture_id"], unique=True,
        postgresql_where=sa.text("status = 'ready'"),
    )  # fmt: skip

    t = "interview_scorecard_drafts"
    op.create_table(
        t,
        *_base(t),
        sa.Column("capture_id", sa.Uuid(), nullable=False),
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("ratings", postgresql.JSONB(), nullable=False),
        sa.Column("answers", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("recommendation", sa.String(12), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'ready'"), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "capture_id", "participant_id", name="uq_interview_scorecard_drafts_participant"
        ),
        _fk("capture_id", "interview_captures", "fk_interview_scorecard_drafts_capture_id", "CASCADE"),
        _fk(
            "participant_id",
            "interview_participants",
            "fk_interview_scorecard_drafts_participant_id",
            "CASCADE",
        ),
        _fk("user_id", "users", "fk_interview_scorecard_drafts_user_id_users"),
        _ck(t, "recommendation_empty", "recommendation IS NULL"),
        _ck(t, "status", "status IN ('ready', 'opened', 'applied', 'dismissed', 'superseded')"),
    )
    op.create_index("ix_interview_scorecard_drafts_tenant_id", t, ["tenant_id"])
    op.create_index("ix_interview_scorecard_drafts_interview_id", t, ["interview_id"])
    op.create_index("ix_interview_scorecard_drafts_user_id", t, ["user_id"])

    t = "scorecard_suggestion_decisions"
    op.create_table(
        t,
        *_base(t),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("competency_key", sa.String(80), nullable=False),
        sa.Column("suggested_rating", sa.SmallInteger(), nullable=False),
        sa.Column("final_rating", sa.SmallInteger(), nullable=True),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "draft_id", "competency_key", name="uq_scorecard_suggestion_decisions_competency"
        ),
        _fk(
            "draft_id", "interview_scorecard_drafts", "fk_scorecard_suggestion_decisions_draft_id", "CASCADE"
        ),
        _ck(t, "decision", "decision IN ('accepted', 'changed', 'ignored')"),
    )
    op.create_index("ix_scorecard_suggestion_decisions_tenant_id", t, ["tenant_id"])

    for table in TABLES:
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
