# ruff: noqa: E501
"""Epic 18 205_03: ``client_user_delegations`` (FR-102 out-of-office delegates; org RLS on ``client_id``).

Revision ID: 20261005_205_03_cp_approvals
Revises: 20261005_205_02_cp_hiring
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_205_03_cp_approvals"
down_revision: str | tuple[str, ...] | None = "20261005_205_02_cp_hiring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "client_user_delegations"


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
        sa.Column("client_user_id", sa.Uuid(), nullable=False),
        sa.Column("delegate_client_user_id", sa.Uuid(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("ends_on >= starts_on", name=op.f(f"ck_{T}_dates")),
        sa.CheckConstraint("delegate_client_user_id <> client_user_id", name=op.f(f"ck_{T}_not_self")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{T}_tenant_id_tenants", ondelete="RESTRICT"),
        _tfk("client_id", "clients"),
        _tfk("client_user_id", "client_users", "CASCADE"),
        _tfk("delegate_client_user_id", "client_users", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{T}"),
    )
    op.create_index(f"ix_{T}_tenant_id", T, ["tenant_id"])
    op.create_index(f"ix_{T}_user", T, ["client_user_id"])
    op.create_index(f"ix_{T}_delegate", T, ["delegate_client_user_id"])
    enable_rls(T)
    enable_org_rls(T, client_col="client_id")


def downgrade() -> None:
    disable_org_rls(T)
    disable_rls(T)
    op.drop_table(T)
