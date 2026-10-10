# ruff: noqa: E501
"""Epic 24 (Bench Management) 211_02: bench-first matching bookkeeping (FR-126).

``bench_match_reviews`` (reviewed / dismissed per demand x candidate), ``bench_match_notices`` (one strong-match
notification per pair, idempotent) and ``bench_match_review_completions`` ("bench Matches reviewed" per demand,
the ``bench.matches_reviewed`` workflow condition). Internal-only org RLS.

Revision ID: 20261005_211_02_bench_matching
Revises: 20261005_211_01_bench_entries
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_211_02_bench_matching"
down_revision: str | tuple[str, ...] | None = "20261005_211_01_bench_entries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base(table: str, *, candidate: bool = True) -> list[object]:
    cols: list[object] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name=f"fk_{table}_demand_id_demands",
            ondelete="CASCADE",
        ),
    ]
    if candidate:
        cols += [
            sa.Column("candidate_id", sa.Uuid(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "candidate_id"],
                ["candidates.tenant_id", "candidates.id"],
                name=f"fk_{table}_candidate_id_candidates",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("tenant_id", "demand_id", "candidate_id", name=f"uq_{table}_pair"),
        ]
    return cols


TABLES = ("bench_match_reviews", "bench_match_notices", "bench_match_review_completions")


def upgrade() -> None:
    op.create_table(
        "bench_match_reviews",
        *_base("bench_match_reviews"),  # type: ignore[arg-type]
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('reviewed', 'dismissed')", name="ck_bench_match_reviews_decision"),
        sa.CheckConstraint(
            "decision <> 'dismissed' OR reason IS NOT NULL", name="ck_bench_match_reviews_dismiss_reason"
        ),
    )
    op.create_table(
        "bench_match_notices",
        *_base("bench_match_notices"),  # type: ignore[arg-type]
        sa.Column("overall", sa.Numeric(6, 2), nullable=False),
    )
    op.create_table(
        "bench_match_review_completions",
        *_base("bench_match_review_completions", candidate=False),  # type: ignore[arg-type]
        sa.Column("completed_by", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "demand_id", name="uq_bench_match_review_completions_demand"),
    )
    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
