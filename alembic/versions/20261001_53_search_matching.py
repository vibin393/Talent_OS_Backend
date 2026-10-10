"""Epic 5 matching: ``matches``, ``match_runs``, ``matching_settings`` (tenant-owned, RLS).

Revision ID: 20261001_53
Revises: 20261001_52
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_53"
down_revision: str | tuple[str, ...] | None = "20261001_52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXPLANATION = "explanation_status IN ('pending', 'ready', 'failed', 'skipped')"
DISMISS = (
    "dismiss_reason IS NULL OR dismiss_reason IN ('not_interested', 'skills_gap', 'experience', 'compensation', "
    "'location', 'availability', 'duplicate', 'other')"
)
TRIGGER = "trigger IN ('opened', 'requirements_changed', 'manual', 'sweep', 'backfill')"
STATUS = "status IN ('running', 'completed', 'failed', 'skipped')"


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=op.f(f"fk_{table}_tenant_id_tenants"), ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_table(
        "matches",
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("overall", sa.Numeric(5, 1), nullable=False),
        sa.Column("sub_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "evidence", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "missing_mandatory", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column("scoring_version", sa.String(length=20), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("explanation_status", sa.String(length=10), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("explanation_action_id", sa.Uuid(), nullable=True),
        sa.Column("explanation_input_hash", sa.String(length=64), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_by", sa.Uuid(), nullable=True),
        sa.Column("dismiss_reason", sa.String(length=20), nullable=True),
        sa.Column("dismiss_note", sa.Text(), nullable=True),
        sa.Column("added_to_pipeline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("is_stale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(EXPLANATION, name=op.f("ck_matches_explanation_status")),
        sa.CheckConstraint(DISMISS, name=op.f("ck_matches_dismiss_reason")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_matches_demand_id_demands",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_matches_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        _tenant_fk("matches"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_matches")),
        sa.UniqueConstraint("demand_id", "candidate_id", name=op.f("uq_matches_demand_id_candidate_id")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_matches_tenant_id_id")),
    )
    op.create_index(op.f("ix_matches_tenant_id"), "matches", ["tenant_id"])
    op.create_index(
        "ix_matches_tenant_id_demand_id_overall",
        "matches",
        ["tenant_id", "demand_id", sa.text("overall DESC")],
        postgresql_where=sa.text("dismissed_at IS NULL AND NOT is_stale"),
    )
    op.create_index("ix_matches_tenant_id_candidate_id", "matches", ["tenant_id", "candidate_id"])
    enable_rls("matches")

    op.create_table(
        "match_runs",
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("considered", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("funnel", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("error", sa.String(length=200), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(TRIGGER, name=op.f("ck_match_runs_trigger")),
        sa.CheckConstraint(STATUS, name=op.f("ck_match_runs_status")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_match_runs_demand_id_demands",
            ondelete="CASCADE",
        ),
        _tenant_fk("match_runs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match_runs")),
    )
    op.create_index(op.f("ix_match_runs_tenant_id"), "match_runs", ["tenant_id"])
    op.create_index(
        "ix_match_runs_tenant_id_demand_id_started_at",
        "match_runs",
        ["tenant_id", "demand_id", sa.text("started_at DESC")],
    )
    enable_rls("match_runs")

    op.create_table(
        "matching_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("weights", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("strong_threshold", sa.Integer(), server_default=sa.text("70"), nullable=False),
        sa.Column("relevant_threshold", sa.Integer(), server_default=sa.text("40"), nullable=False),
        sa.Column("proxy_fields", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("explain_top_n", sa.Integer(), server_default=sa.text("25"), nullable=False),
        sa.Column(
            "fx_rates", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("strong_threshold BETWEEN 0 AND 100", name=op.f("ck_matching_settings_strong_threshold")),
        sa.CheckConstraint(
            "relevant_threshold BETWEEN 0 AND 100", name=op.f("ck_matching_settings_relevant_threshold")
        ),
        sa.CheckConstraint("explain_top_n BETWEEN 0 AND 100", name=op.f("ck_matching_settings_explain_top_n")),
        _tenant_fk("matching_settings"),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_matching_settings")),
    )
    enable_rls("matching_settings")


def downgrade() -> None:
    for table in ("matching_settings", "match_runs", "matches"):
        disable_rls(table)
        op.drop_table(table)
