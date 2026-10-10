# ruff: noqa: E501
"""Epic 26 (Leave & Holidays) 213_03: ``leave_requests`` (FR-136, §5.10) with org RLS (Client sees its
Workers' leave, the Worker sees their own).

Revision ID: 20261005_213_03_leave_requests
Revises: 20261005_213_02_leave_types_bal
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_213_03_leave_requests"
down_revision: str | tuple[str, ...] | None = "20261005_213_02_leave_types_bal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tfk(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_leave_requests_{column}_{target}"[:63],
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    op.create_table(
        "leave_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("leave_year", sa.String(10), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("start_half", sa.String(2), nullable=True),
        sa.Column("end_half", sa.String(2), nullable=True),
        sa.Column("days", sa.Numeric(6, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), server_default=sa.text("'requested'"), nullable=False),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by_kind", sa.String(20), server_default=sa.text("'candidate'"), nullable=False),
        sa.Column("requested_by_id", sa.Uuid(), nullable=True),
        sa.Column("proxy_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('requested', 'approved', 'rejected', 'withdrawn', 'cancellation_requested', 'cancelled')",
            name="ck_leave_requests_status",
        ),
        sa.CheckConstraint("end_date >= start_date", name="ck_leave_requests_dates"),
        sa.CheckConstraint("start_half IS NULL OR start_half = 'pm'", name="ck_leave_requests_start_half"),
        sa.CheckConstraint("end_half IS NULL OR end_half = 'am'", name="ck_leave_requests_end_half"),
        sa.CheckConstraint("days > 0", name="ck_leave_requests_days"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_leave_requests_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["approval_request_id"],
            ["approval_requests.id"],
            name="fk_leave_requests_approval_request_id",
            ondelete="SET NULL",
        ),
        _tfk("candidate_id", "candidates"),
        _tfk("placement_id", "placements"),
        _tfk("client_id", "clients"),
        _tfk("legal_entity_id", "legal_entities"),
        _tfk("leave_type_id", "leave_types"),
        sa.PrimaryKeyConstraint("id", name="pk_leave_requests"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_leave_requests_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_leave_requests_tenant_id_human_id"),
    )
    op.create_index("ix_leave_requests_tenant_id", "leave_requests", ["tenant_id"])
    op.create_index("ix_leave_requests_candidate_id", "leave_requests", ["candidate_id", "start_date"])
    op.create_index("ix_leave_requests_placement_id", "leave_requests", ["placement_id", "start_date"])
    op.create_index("ix_leave_requests_client_id", "leave_requests", ["client_id", "start_date"])
    op.create_index("ix_leave_requests_status", "leave_requests", ["status"])
    enable_rls("leave_requests")
    enable_org_rls("leave_requests", client_col="client_id", worker_col="candidate_id")


def downgrade() -> None:
    disable_org_rls("leave_requests")
    disable_rls("leave_requests")
    op.drop_table("leave_requests")
