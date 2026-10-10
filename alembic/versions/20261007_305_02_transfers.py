# ruff: noqa: E501
"""Epic 41 story 41.3 (FR-211): ``internal_transfers`` -- the human-confirmed (*) transfer of an Employee.

Worker org RLS on ``candidate_id`` (a Worker portal session sees only its own transfers).

Revision ID: 20261007_305_02_transfers
Revises: 20261007_305_01_mobility
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_305_02_transfers"
down_revision: str | tuple[str, ...] | None = "20261007_305_01_mobility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fk(name: str, col: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete="RESTRICT"
    )


def upgrade() -> None:
    t = "internal_transfers"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("mobility_application_id", sa.Uuid(), nullable=False),
        sa.Column("employee_kind", sa.String(10), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("from_placement_id", sa.Uuid(), nullable=True),
        sa.Column("to_placement_id", sa.Uuid(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("from_team_id", sa.Uuid(), nullable=True),
        sa.Column("to_team_id", sa.Uuid(), nullable=True),
        sa.Column("to_role_title", sa.String(300), nullable=True),
        sa.Column("confirmed_by", sa.Uuid(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("mobility_application_id", name="uq_internal_transfers_mobility_app"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.CheckConstraint("employee_kind IN ('user', 'worker')", name=op.f("ck_internal_transfers_employee_kind")),
        _fk("fk_internal_transfers_mobility_app", "mobility_application_id", "mobility_applications"),
        _fk("fk_internal_transfers_candidate_id", "candidate_id", "candidates"),
        _fk("fk_internal_transfers_user_id", "user_id", "users"),
        _fk("fk_internal_transfers_from_placement", "from_placement_id", "placements"),
        _fk("fk_internal_transfers_to_placement", "to_placement_id", "placements"),
        _fk("fk_internal_transfers_from_team_id", "from_team_id", "teams"),
        _fk("fk_internal_transfers_to_team_id", "to_team_id", "teams"),
        _fk("fk_internal_transfers_confirmed_by", "confirmed_by", "users"),
    )
    op.create_index("ix_internal_transfers_tenant_id", t, ["tenant_id"])
    op.create_index("ix_internal_transfers_candidate_id", t, ["candidate_id"])
    enable_rls(t)
    enable_org_rls(t, worker_col="candidate_id")


def downgrade() -> None:
    disable_org_rls("internal_transfers")
    disable_rls("internal_transfers")
    op.drop_table("internal_transfers")
