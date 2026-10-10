# ruff: noqa: E501
"""Epic 44 (workforce planning, FR-224): ``headcount_slots``.

One slot per unit of approved line headcount, keyed by the line lineage (``plan_line_id`` = the line's first row,
stable across revisions). ``demand_id`` is unique when set (one draft Demand per slot). Internal only (org RLS
without columns).

Revision ID: 20261007_308_02_wf_slots
Revises: 20261007_308_01_wf_plans
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_308_02_wf_slots"
down_revision: str | tuple[str, ...] | None = "20261007_308_01_wf_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fk(column: str, target: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete="RESTRICT"
    )


def upgrade() -> None:
    t = "headcount_slots"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("workforce_plan_id", sa.Uuid(), nullable=False),
        sa.Column("plan_line_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(15), server_default=sa.text("'open'"), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=True),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requisitioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_headcount_slots"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_headcount_slots_tenant_id_id"),
        sa.UniqueConstraint("plan_line_id", "seq", name="uq_headcount_slots_line_seq"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_headcount_slots_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _fk("workforce_plan_id", "workforce_plans", "fk_headcount_slots_workforce_plan_id_plans"),
        _fk("plan_line_id", "workforce_plan_lines", "fk_headcount_slots_plan_line_id_lines"),
        _fk("demand_id", "demands", "fk_headcount_slots_demand_id_demands"),
        _fk("placement_id", "placements", "fk_headcount_slots_placement_id_placements"),
        sa.CheckConstraint(
            "status IN ('open', 'requisitioned', 'filled', 'cancelled')",
            name=op.f("ck_headcount_slots_status"),
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_headcount_slots_seq")),
    )
    for cols in (("tenant_id",), ("workforce_plan_id",), ("plan_line_id",), ("placement_id",)):
        op.create_index(f"ix_{t}_{'_'.join(cols)}", t, list(cols))
    op.create_index(
        "uq_headcount_slots_demand_id",
        t,
        ["demand_id"],
        unique=True,
        postgresql_where=sa.text("demand_id IS NOT NULL"),
    )
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    t = "headcount_slots"
    disable_org_rls(t)
    disable_rls(t)
    op.drop_table(t)
