# ruff: noqa: E501
"""Sourcing & Matching Agent (Epic 56, stories 56.1 / 56.2 / 56.5): agent shortlists, entries, dismissals and the
per-tenant sourcing settings.

Every table: tenant RLS + internal-only org RLS.

Revision ID: 20261008_403_01_src_shortlists
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_403_01_src_shortlists"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_sourcing_agent",)
depends_on: str | Sequence[str] | None = None

TABLES = (
    "agent_shortlists",
    "agent_shortlist_entries",
    "agent_shortlist_dismissals",
    "sourcing_agent_settings",
)
TRIGGER_KEYS = ("demand_opened", "requirements_changed", "weekly_refresh", "manual", "workflow")
DISMISS_REASONS = (
    "not_interested", "skills_gap", "experience", "compensation", "location", "availability", "duplicate", "other",
)  # fmt: skip
CHANNELS = ("email", "sms", "whatsapp")


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


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def upgrade() -> None:
    t = "agent_shortlists"
    op.create_table(
        t,
        *_base(t),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'current'"), nullable=False),
        sa.Column("trigger_key", sa.String(40), nullable=False),
        sa.Column("considered_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _jsonb("excluded_counts", "{}"),
        _jsonb("suppressed_counts", "{}"),
        sa.Column("top_n", sa.SmallInteger(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "demand_id", "version", name="uq_agent_shortlists_tenant_id_demand_id_version"
        ),
        _fk("demand_id", "demands", "fk_agent_shortlists_demand_id_demands"),
        _fk("run_id", "agent_runs", "fk_agent_shortlists_run_id_agent_runs"),
        _ck(t, "status", "status IN ('current', 'superseded')"),
        _ck(t, "trigger_key", _in("trigger_key", TRIGGER_KEYS)),
        _ck(t, "top_n", "top_n BETWEEN 5 AND 100"),
        _ck(t, "summary_length", "summary IS NULL OR char_length(summary) <= 2000"),
    )
    op.create_index("ix_agent_shortlists_tenant_id", t, ["tenant_id"])
    op.create_index(
        "uq_agent_shortlists_current", t, ["tenant_id", "demand_id"], unique=True,
        postgresql_where=sa.text("status = 'current'"),
    )  # fmt: skip

    t = "agent_shortlist_entries"
    op.create_table(
        t,
        *_base(t),
        sa.Column("shortlist_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.Column("overall", sa.Numeric(5, 1), nullable=True),
        sa.Column("match_id", sa.Uuid(), nullable=True),
        sa.Column("sources", postgresql.JSONB(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("agent_note", sa.String(300), nullable=True),
        _jsonb("flags", "[]"),
        sa.Column("contactability", postgresql.JSONB(), nullable=False),
        sa.Column("best_channel", sa.String(10), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notice_period_days", sa.Integer(), nullable=True),
        sa.Column("availability_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'suggested'"), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismiss_reason", sa.String(30), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("draft_channel", sa.String(10), nullable=True),
        sa.Column("draft_subject", sa.String(200), nullable=True),
        sa.Column("draft_body", sa.Text(), nullable=True),
        sa.Column("draft_status", sa.String(12), server_default=sa.text("'none'"), nullable=False),
        sa.Column("outreach_proposal_id", sa.Uuid(), nullable=True),
        sa.Column("outreach_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "shortlist_id",
            "candidate_id",
            name="uq_agent_shortlist_entries_tenant_id_shortlist_id_candidate_id",
        ),
        sa.UniqueConstraint(
            "tenant_id", "shortlist_id", "rank", name="uq_agent_shortlist_entries_tenant_id_shortlist_id_rank"
        ),
        _fk(
            "shortlist_id", "agent_shortlists", "fk_agent_shortlist_entries_shortlist_id", ondelete="CASCADE"
        ),
        _fk("candidate_id", "candidates", "fk_agent_shortlist_entries_candidate_id"),
        _ck(t, "status", "status IN ('suggested', 'added_to_pipeline', 'dismissed')"),
        _ck(t, "draft_status", "draft_status IN ('none', 'proposed', 'sent', 'failed', 'suppressed')"),
        _ck(t, "dismiss_reason", "status <> 'dismissed' OR dismiss_reason IS NOT NULL"),
        _ck(t, "dismiss_reason_value", f"dismiss_reason IS NULL OR {_in('dismiss_reason', DISMISS_REASONS)}"),
        _ck(t, "best_channel", f"best_channel IS NULL OR {_in('best_channel', CHANNELS)}"),
        _ck(t, "draft_channel", f"draft_channel IS NULL OR {_in('draft_channel', CHANNELS)}"),
        _ck(t, "draft_body_length", "draft_body IS NULL OR char_length(draft_body) <= 4000"),
        _ck(t, "explanation_length", "explanation IS NULL OR char_length(explanation) <= 400"),
    )
    op.create_index("ix_agent_shortlist_entries_tenant_id", t, ["tenant_id"])
    op.create_index("ix_agent_shortlist_entries_candidate_id", t, ["candidate_id"])
    op.create_index("ix_agent_shortlist_entries_demand_candidate", t, ["demand_id", "candidate_id"])
    op.create_index("ix_agent_shortlist_entries_candidate_sent", t, ["candidate_id", "outreach_sent_at"])

    t = "agent_shortlist_dismissals"
    op.create_table(
        t,
        *_base(t),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(30), nullable=False),
        sa.Column("dismissed_by", sa.Uuid(), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "demand_id",
            "candidate_id",
            name="uq_agent_shortlist_dismissals_tenant_id_demand_id_candidate_id",
        ),
        _fk("demand_id", "demands", "fk_agent_shortlist_dismissals_demand_id"),
        _fk("candidate_id", "candidates", "fk_agent_shortlist_dismissals_candidate_id"),
        _ck(t, "reason", _in("reason", DISMISS_REASONS)),
    )
    op.create_index("ix_agent_shortlist_dismissals_tenant_id", t, ["tenant_id"])
    op.create_index("ix_agent_shortlist_dismissals_candidate_id", t, ["candidate_id"])

    t = "sourcing_agent_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("top_n", sa.SmallInteger(), server_default=sa.text("25"), nullable=False),
        sa.Column("draft_top", sa.SmallInteger(), server_default=sa.text("10"), nullable=False),
        sa.Column("preferred_channel", sa.String(10), server_default=sa.text("'email'"), nullable=False),
        sa.Column("include_silver_medalists", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("max_searches", sa.SmallInteger(), server_default=sa.text("3"), nullable=False),
        sa.Column("propose_pipeline_adds", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("propose_sequence_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _fk("propose_sequence_id", "sequences", "fk_sourcing_agent_settings_sequence_id"),
        _ck(t, "top_n", "top_n BETWEEN 5 AND 100"),
        _ck(t, "draft_top", "draft_top BETWEEN 0 AND 25"),
        _ck(t, "max_searches", "max_searches BETWEEN 0 AND 5"),
        _ck(t, "preferred_channel", _in("preferred_channel", CHANNELS)),
    )
    op.create_index("ix_sourcing_agent_settings_tenant_id", t, ["tenant_id"])

    for table in TABLES:
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
