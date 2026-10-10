# ruff: noqa: E501
"""Epic 19 (Placements) 206_03: worker profiles, worker bank accounts (encrypted, AD-35) and
``offers.placement_dropped_at`` (P1 table, note 18).

Revision ID: 20261004_206_03_workers
Revises: 20261004_206_02_gates_prejoining
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261004_206_03_workers"
down_revision: str | tuple[str, ...] | None = "20261004_206_02_gates_prejoining"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base(table: str) -> list[object]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name=f"fk_{table}_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "worker_profiles",
        *_base("worker_profiles"),  # type: ignore[arg-type]
        sa.Column("worker_human_id", sa.String(20), nullable=False),
        sa.Column("default_worker_type", sa.String(20), nullable=True),
        sa.Column("manager_user_id", sa.Uuid(), nullable=True),
        sa.Column("emergency_name", sa.String(200), nullable=True),
        sa.Column("emergency_relation", sa.String(100), nullable=True),
        sa.Column("emergency_phone", sa.String(40), nullable=True),
        sa.Column("pan", sa.Text(), nullable=True),
        sa.Column("pan_last4", sa.String(4), nullable=True),
        sa.Column("pan_bidx", sa.String(32), nullable=True),
        sa.Column("pan_name", sa.String(200), nullable=True),
        sa.Column("pan_status", sa.String(20), server_default=sa.text("'unverified'"), nullable=False),
        sa.CheckConstraint("pan_status IN ('unverified', 'hr_checked')", name="ck_worker_profiles_pan_status"),
        sa.UniqueConstraint("tenant_id", "candidate_id", name="uq_worker_profiles_candidate_id"),
        sa.UniqueConstraint("tenant_id", "worker_human_id", name="uq_worker_profiles_worker_human_id"),
    )
    op.create_index("ix_worker_profiles_tenant_id", "worker_profiles", ["tenant_id"])

    op.create_table(
        "worker_bank_accounts",
        *_base("worker_bank_accounts"),  # type: ignore[arg-type]
        sa.Column("holder_name", sa.String(200), nullable=True),
        sa.Column("account_number", sa.Text(), nullable=True),
        sa.Column("account_number_last4", sa.String(4), nullable=True),
        sa.Column("account_number_bidx", sa.String(32), nullable=True),
        sa.Column("ifsc", sa.String(11), nullable=False),
        sa.Column("bank_name", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending_approval'"), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'active', 'superseded', 'rejected')", name="ck_worker_bank_accounts_status"
        ),
        sa.CheckConstraint(
            "source IN ('onboarding', 'worker_portal', 'user', 'import')", name="ck_worker_bank_accounts_source"
        ),
    )
    op.create_index("ix_worker_bank_accounts_tenant_id", "worker_bank_accounts", ["tenant_id"])
    op.create_index("ix_worker_bank_accounts_candidate_id", "worker_bank_accounts", ["candidate_id"])
    op.create_index(
        "uq_worker_bank_accounts_active",
        "worker_bank_accounts",
        ["tenant_id", "candidate_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    for table in ("worker_profiles", "worker_bank_accounts"):
        enable_rls(table)
        enable_org_rls(table, worker_col="candidate_id")

    op.add_column("offers", sa.Column("placement_dropped_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("offers", "placement_dropped_at")
    for table in ("worker_bank_accounts", "worker_profiles"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
