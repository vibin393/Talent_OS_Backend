# ruff: noqa: E501
"""Epic 45 (FR-230): ``project_budgets`` and the ``project_burn`` read model (internal only).

Budgets: one row per project and dimension (``hours`` | ``cost`` | ``revenue``); ``version`` bumps on edit and
the 80 / 100 % alert stamps reset. Burn: one row per project and dimension rebuilt from posted facts (AD-33) with
a ``source_hash`` so a rebuild without new facts writes nothing. Org RLS without columns = internal sessions only.

Revision ID: 20261007_309_03_proj_budgets
Revises: 20261007_309_02_deliverables
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_309_03_proj_budgets"
down_revision: str | tuple[str, ...] | None = "20261007_309_02_deliverables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIMENSION = "dimension IN ('hours', 'cost', 'revenue')"


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("dimension", sa.String(10), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.UniqueConstraint("project_id", "dimension", name=f"uq_{table}_project_id_dimension"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=f"fk_{table}_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_DIMENSION, name=op.f(f"ck_{table}_dimension")),
    ]


def upgrade() -> None:
    t = "project_budgets"
    op.create_table(
        t,
        *_base(t),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("alert_80_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alert_100_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_project_budgets_amount")),
        sa.CheckConstraint(
            "(dimension = 'hours') = (currency IS NULL)", name=op.f("ck_project_budgets_currency")
        ),
    )
    op.create_index("ix_project_budgets_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)

    t = "project_burn"
    op.create_table(
        t,
        *_base(t),
        sa.Column("actual", sa.Numeric(16, 2), nullable=False),
        sa.Column("budget", sa.Numeric(16, 2), nullable=True),
        sa.Column("pct", sa.Numeric(7, 4), nullable=True),
        sa.Column("burn_rate_per_day", sa.Numeric(16, 2), nullable=False),
        sa.Column("forecast_at_completion", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("breakdown", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("source_hash", sa.CHAR(64), nullable=False),
    )
    op.create_index("ix_project_burn_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    for t in ("project_burn", "project_budgets"):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
