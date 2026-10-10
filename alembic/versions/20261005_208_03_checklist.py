# ruff: noqa: E501
"""Epic 21 (Compliance) 208_03: the per-Placement compliance checklist and requirement re-evaluation runs.

``placement_compliance_items`` inherits ``candidate_id`` from its placement (org-RLS trigger) and admits the
internal and worker org kinds only: Client / Vendor Users get **no** pass in Phase 2 (RESTRICTIVE policy).

Revision ID: 20261005_208_03_checklist
Revises: 20261005_208_02_credentials
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_208_03_checklist"
down_revision: str | tuple[str, ...] | None = "20261005_208_02_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("placement_compliance_items", "compliance_evaluation_runs")


def upgrade() -> None:
    op.create_table(
        "placement_compliance_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("requirement_ids", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'::uuid[]"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("ref", sa.String(100), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("mandatory", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'missing'"), nullable=False),
        sa.Column("retroactive", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=True),
        sa.Column("waived_reason", sa.Text(), nullable=True),
        sa.Column("waived_by", sa.Uuid(), nullable=True),
        sa.Column("waived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_placement_compliance_items_tenant_id_tenants", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_placement_compliance_items"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_placement_compliance_items_tenant_id_id"),
        sa.UniqueConstraint("placement_id", "kind", "ref", name="uq_placement_compliance_items_key"),
        sa.CheckConstraint("kind IN ('credential', 'document', 'bgv_package', 'policy_ack')", name="ck_placement_compliance_items_kind"),
        sa.CheckConstraint(
            "status IN ('missing', 'pending', 'met', 'expiring', 'expired', 'waived')", name="ck_placement_compliance_items_status"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "placement_id"],
            ["placements.tenant_id", "placements.id"],
            name="fk_placement_compliance_items_placement_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_placement_compliance_items_tenant_id", "placement_compliance_items", ["tenant_id"])
    op.create_index("ix_placement_compliance_items_placement_id", "placement_compliance_items", ["placement_id"])
    op.create_index("ix_placement_compliance_items_candidate_id", "placement_compliance_items", ["candidate_id"])

    op.create_table(
        "compliance_evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("requirement_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("placements_evaluated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("newly_non_compliant", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'::uuid[]"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_compliance_evaluation_runs_tenant_id_tenants", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_compliance_evaluation_runs"),
    )
    op.create_index("ix_compliance_evaluation_runs_tenant_id", "compliance_evaluation_runs", ["tenant_id"])

    for table in TABLES:
        enable_rls(table)
    enable_org_rls(
        "placement_compliance_items",
        worker_col="candidate_id",
        inherit_from=InheritRef("placements", "placement_id", ("candidate_id",)),
    )
    enable_org_rls("compliance_evaluation_runs")


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
