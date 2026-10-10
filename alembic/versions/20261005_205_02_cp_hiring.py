# ruff: noqa: E501
"""Epic 18 205_02: ``interview_slot_proposals`` (FR-101; org RLS on ``client_id``).

Revision ID: 20261005_205_02_cp_hiring
Revises: 20261005_205_01_cp_org_rls
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_205_02_cp_hiring"
down_revision: str | tuple[str, ...] | None = "20261005_205_01_cp_org_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "interview_slot_proposals"


def _tfk(column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=f"fk_{T}_{column}_{target}"[:63], ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        T,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=True),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("slots", postgresql.JSONB(), nullable=False),
        sa.Column("chosen_index", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("created_by_kind", sa.String(20), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("interview_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('client_proposed', 'recruiter_offered')", name=op.f(f"ck_{T}_kind")),
        sa.CheckConstraint("status IN ('open', 'chosen', 'scheduled', 'declined', 'expired')", name=op.f(f"ck_{T}_status")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{T}_tenant_id_tenants", ondelete="RESTRICT"),
        _tfk("client_id", "clients"),
        _tfk("application_id", "applications", "CASCADE"),
        _tfk("demand_id", "demands"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{T}"),
    )
    op.create_index(f"ix_{T}_tenant_id", T, ["tenant_id"])
    op.create_index(f"ix_{T}_application_id", T, ["application_id"])
    op.create_index(f"ix_{T}_client_id", T, ["client_id", "status"])
    enable_rls(T)
    enable_org_rls(T, client_col="client_id")


def downgrade() -> None:
    disable_org_rls(T)
    disable_rls(T)
    op.drop_table(T)
