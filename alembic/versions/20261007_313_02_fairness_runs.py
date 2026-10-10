# ruff: noqa: E501
"""Epic 49 stories 49.3 / 49.4 (FR-247, FR-248): ``fairness_runs`` (aggregate-only snapshots), ``aedt_inventory``
(+ seed rows per tenant, ``inactive`` until a Compliance user confirms) and ``fairness_publications``.

All three are internal-only (tenant RLS + org policy without portal branches).

Revision ID: 20261007_313_02_fairness_runs
Revises: 20261007_313_01_fairness_core
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_313_02_fairness_runs"
down_revision: str | tuple[str, ...] | None = "20261007_313_01_fairness_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_AEDT = (
    (
        "ai_match",
        "ai_match",
        "AI Match",
        "Candidate profile (skills, experience, location, compensation fit) against Demand requirements",
    ),
    (
        "ai_screening",
        "ai_screening",
        "AI Screening",
        "Candidate answers to screening questions and the resume text",
    ),
    (
        "assessment_autoscore",
        "assessment_autoscore",
        "Assessment auto-scoring",
        "Candidate answers to objective assessment items",
    ),
)


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
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
    t = "fairness_runs"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("selection_point", sa.String(30), nullable=False),
        sa.Column("params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("filters", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("intersections", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("k", sa.SmallInteger(), nullable=False),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("total_applicants", sa.Integer(), nullable=True),
        sa.Column("unknown_counts", postgresql.JSONB(), nullable=True),
        sa.Column("pack", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(60), nullable=True),
        sa.CheckConstraint(
            "selection_point IN ('stage_transition', 'ai_match_threshold', 'ai_screening_pass', 'assessment_pass')",
            name=op.f("ck_fairness_runs_selection_point"),
        ),
        sa.CheckConstraint(
            "method IN ('selection_rate', 'scoring_rate', 'both')", name=op.f("ck_fairness_runs_method")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')", name=op.f("ck_fairness_runs_status")
        ),
        sa.CheckConstraint("k >= 5", name=op.f("ck_fairness_runs_k")),
        sa.CheckConstraint("date_from <= date_to", name=op.f("ck_fairness_runs_dates")),
        _fk(t, "created_by", "users"),
    )
    op.create_index("ix_fairness_runs_tenant_id", t, ["tenant_id"])
    op.create_index("ix_fairness_runs_created_at", t, ["created_at"])

    t = "aedt_inventory"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("feature", sa.String(30), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.String(60), nullable=True),
        sa.Column("first_used_on", sa.Date(), nullable=True),
        sa.Column("data_used", sa.Text(), nullable=True),
        sa.Column(
            "job_categories",
            postgresql.ARRAY(sa.String(200)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("used_to_filter", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'inactive'"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("seed_key", sa.String(40), nullable=True),
        *_ts(),
        sa.CheckConstraint(
            "feature IN ('ai_match', 'ai_screening', 'assessment_autoscore', 'other')",
            name=op.f("ck_aedt_inventory_feature"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'retired', 'inactive')", name=op.f("ck_aedt_inventory_status")
        ),
    )
    op.create_index("ix_aedt_inventory_tenant_id", t, ["tenant_id"])
    op.create_index(
        "uq_aedt_inventory_seed_key",
        t,
        ["tenant_id", "seed_key"],
        unique=True,
        postgresql_where=sa.text("seed_key IS NOT NULL"),
    )
    values = ", ".join(f"('{k}', '{f}', '{n}', '{d}')" for k, f, n, d in SEED_AEDT)
    op.execute(
        "INSERT INTO aedt_inventory (id, tenant_id, seed_key, feature, name, data_used, used_to_filter, status) "
        f"SELECT gen_random_uuid(), t.id, v.k, v.f, v.n, v.d, true, 'inactive' FROM tenants t "
        f"CROSS JOIN (VALUES {values}) AS v(k, f, n, d)"
    )

    t = "fairness_publications"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("aedt_id", sa.Uuid(), nullable=False),
        sa.Column("audit_date", sa.Date(), nullable=False),
        sa.Column("distribution_date", sa.Date(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("unpublished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.CheckConstraint("audit_date <= distribution_date", name=op.f("ck_fairness_publications_dates")),
        _fk(t, "run_id", "fairness_runs"),
        _fk(t, "aedt_id", "aedt_inventory"),
        _fk(t, "published_by", "users"),
    )
    op.create_index("ix_fairness_publications_tenant_id", t, ["tenant_id"])
    for table in ("fairness_runs", "aedt_inventory", "fairness_publications"):
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in ("fairness_publications", "aedt_inventory", "fairness_runs"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
