# ruff: noqa: E501
"""Epic 65 (stories 65.2, 65.5): platform evaluation store and Proposal review signals.

* ``ai_eval_runs`` / ``ai_eval_results`` -- platform tables (no RLS; ``talent_app`` gets SELECT / INSERT / UPDATE).
  Eval data is platform test data, never tenant data (FR-322).
* ``proposal_review_signals`` -- SM-C14 rubber-stamp telemetry per (proposal, user); tenant RLS + internal-only org
  RLS; composite tenant FK to the foundation ``agent_proposals`` (CASCADE).

First revision of branch ``p4_governance`` (band 412) on the Phase 4 foundation head.

Revision ID: 20261009_412_01_p4_gov
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls
from app.core.settings import get_settings

revision: str = "20261009_412_01_p4_gov"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_governance",)
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")
S = "proposal_review_signals"


def upgrade() -> None:
    op.create_table(
        "ai_eval_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("trigger", sa.String(10), nullable=False),
        sa.Column("git_sha", sa.String(40), nullable=True),
        sa.Column("sdk_version", sa.String(20), nullable=True),
        sa.Column("models", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ai_eval_runs"),
        sa.CheckConstraint("mode IN ('offline', 'live')", name=op.f("ck_ai_eval_runs_mode")),
        sa.CheckConstraint(
            "trigger IN ('ci', 'schedule', 'manual', 'release')", name=op.f("ck_ai_eval_runs_trigger")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'passed', 'failed', 'error', 'skipped')",
            name=op.f("ck_ai_eval_runs_status"),
        ),
    )
    op.create_index("ix_ai_eval_runs_created_at", "ai_eval_runs", ["created_at"])
    op.create_table(
        "ai_eval_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("agent_version", sa.Integer(), nullable=False),
        sa.Column("suite", sa.String(10), nullable=False),
        sa.Column("prompt_ref", sa.String(200), nullable=True),
        sa.Column("model", sa.String(60), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("cases_total", sa.Integer(), nullable=False),
        sa.Column("cases_passed", sa.Integer(), nullable=False),
        sa.Column("score", sa.Numeric(5, 4), nullable=False),
        sa.Column("threshold", sa.Numeric(3, 2), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("failures", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ai_eval_results"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["ai_eval_runs.id"], name="fk_ai_eval_results_run_id_ai_eval_runs", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("run_id", "agent_key", "suite", name="uq_ai_eval_results_run_id_agent_key_suite"),
        sa.CheckConstraint("suite IN ('agent', 'redteam')", name=op.f("ck_ai_eval_results_suite")),
        sa.CheckConstraint("score >= 0 AND score <= 1", name=op.f("ck_ai_eval_results_score")),
    )
    op.create_index(
        "ix_ai_eval_results_agent_suite_created", "ai_eval_results", ["agent_key", "suite", "created_at"]
    )
    op.create_index("ix_ai_eval_results_fingerprint", "ai_eval_results", ["fingerprint"])
    role = get_settings().db_app_user
    for t in ("ai_eval_runs", "ai_eval_results"):
        op.execute(f"REVOKE DELETE, TRUNCATE ON {t} FROM {role}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {t} TO {role}")

    op.create_table(
        S,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("first_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(20), nullable=True),
        sa.Column("decision_ms", sa.Integer(), nullable=True),
        sa.Column("rubber_stamp", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{S}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{S}_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "proposal_id",
            "user_id",
            name="uq_proposal_review_signals_tenant_id_proposal_id_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{S}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "proposal_id"],
            ["agent_proposals.tenant_id", "agent_proposals.id"],
            name="fk_proposal_review_signals_proposal_id_agent_proposals",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_proposal_review_signals_user_id_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('approved', 'edited_approved', 'rejected')",
            name=op.f("ck_proposal_review_signals_decision"),
        ),
    )
    op.create_index(f"ix_{S}_tenant_id", S, ["tenant_id"])
    op.create_index("ix_proposal_review_signals_decided", S, ["tenant_id", "decided_at"])
    enable_rls(S)
    enable_org_rls(S)


def downgrade() -> None:
    disable_org_rls(S)
    disable_rls(S)
    op.drop_table(S)
    op.drop_table("ai_eval_results")
    op.drop_table("ai_eval_runs")
