"""Epic 17 204_02: ``vendor_releases`` (FR-94) -- a Demand released to one Vendor with commercial limits and a
snapshot the Vendor Portal reads instead of ``demands`` (AD-26). Org RLS ``vendor_col="vendor_id"``.

Revision ID: 20261004_204_02_vendor_releases
Revises: 20261004_204_01_vendors_core
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261004_204_02_vendor_releases"
down_revision: str | tuple[str, ...] | None = "20261004_204_01_vendors_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def tfk(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_vendor_releases_{column}_{target}",
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    op.create_table(
        "vendor_releases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("reveal_client", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("max_vendor_rate", sa.Numeric(14, 4), nullable=True),
        sa.Column("rate_unit", sa.String(10), nullable=True),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'INR'"), nullable=False),
        sa.Column("submission_limit", sa.Integer(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("released_by", sa.Uuid(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paused_by_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("positions", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("skills", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("location", sa.String(300), nullable=True),
        sa.Column("employment_type", sa.String(20), nullable=True),
        sa.Column("experience_range", sa.String(40), nullable=True),
        sa.Column("jd_public_text", sa.Text(), nullable=True),
        sa.Column("client_display_name", sa.String(300), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_vendor_releases"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_vendor_releases_tenant_id_tenants", ondelete="RESTRICT"
        ),
        tfk("vendor_id", "vendors"),
        tfk("demand_id", "demands"),
        tfk("client_id", "clients"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'withdrawn', 'closed')", name=op.f("ck_vendor_releases_status")
        ),
        sa.CheckConstraint(
            "max_vendor_rate IS NULL OR max_vendor_rate >= 0", name=op.f("ck_vendor_releases_max_rate")
        ),
        sa.CheckConstraint(
            "submission_limit IS NULL OR submission_limit > 0", name=op.f("ck_vendor_releases_limit")
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_vendor_releases_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_vendor_releases_tenant_id_human_id"),
    )
    op.create_index("ix_vendor_releases_tenant_id", "vendor_releases", ["tenant_id"])
    op.create_index("ix_vendor_releases_vendor_id", "vendor_releases", ["vendor_id"])
    op.create_index("ix_vendor_releases_demand_id", "vendor_releases", ["demand_id"])
    op.create_index(
        "uq_vendor_releases_open",
        "vendor_releases",
        ["demand_id", "vendor_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'paused')"),
    )
    enable_rls("vendor_releases")
    enable_org_rls("vendor_releases", vendor_col="vendor_id")


def downgrade() -> None:
    disable_org_rls("vendor_releases")
    disable_rls("vendor_releases")
    op.drop_table("vendor_releases")
