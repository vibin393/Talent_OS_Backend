# ruff: noqa: E501
"""Epic 19 (Placements) 206_02: start gates, start-gate overrides, pre-joining touchpoints.

Revision ID: 20261004_206_02_gates_prejoining
Revises: 20261004_206_01_placements_core
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261004_206_02_gates_prejoining"
down_revision: str | tuple[str, ...] | None = "20261004_206_01_placements_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG = ("client_id", "vendor_id", "candidate_id")


def _base(table: str) -> list[object]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "placement_id"],
            ["placements.tenant_id", "placements.id"],
            name=f"fk_{table}_placement_id_placements",
            ondelete="CASCADE",
        ),
    ]


def _org_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [sa.Column(c, sa.Uuid(), nullable=True) for c in ORG]


def upgrade() -> None:
    op.create_table(
        "start_gates",
        *_base("start_gates"),  # type: ignore[arg-type]
        *_org_columns(),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("source_ref", sa.String(100), nullable=False),
        sa.Column("required", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("detail", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("override_id", sa.Uuid(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'met', 'failed', 'waived', 'not_required')", name="ck_start_gates_status"),
        sa.UniqueConstraint("tenant_id", "placement_id", "kind", "source_ref", name="uq_start_gates_key"),
    )
    op.create_index("ix_start_gates_tenant_id", "start_gates", ["tenant_id"])
    op.create_index("ix_start_gates_placement_id", "start_gates", ["placement_id"])

    op.create_table(
        "start_gate_overrides",
        *_base("start_gate_overrides"),  # type: ignore[arg-type]
        sa.Column("gate_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_start_gate_overrides_status"),
    )
    op.create_index("ix_start_gate_overrides_tenant_id", "start_gate_overrides", ["tenant_id"])
    op.create_index("ix_start_gate_overrides_placement_id", "start_gate_overrides", ["placement_id"])

    op.create_table(
        "placement_touchpoints",
        *_base("placement_touchpoints"),  # type: ignore[arg-type]
        *_org_columns(),
        sa.Column("offset_days", sa.Integer(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("secure_link_id", sa.Uuid(), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response", sa.String(20), nullable=True),
        sa.Column("response_note", sa.Text(), nullable=True),
        sa.CheckConstraint("response IS NULL OR response IN ('confirmed', 'question')", name="ck_placement_touchpoints_response"),
        sa.UniqueConstraint("tenant_id", "placement_id", "offset_days", name="uq_placement_touchpoints_offset"),
    )
    op.create_index("ix_placement_touchpoints_tenant_id", "placement_touchpoints", ["tenant_id"])
    op.create_index("ix_placement_touchpoints_placement_id", "placement_touchpoints", ["placement_id"])

    for table in ("start_gates", "start_gate_overrides", "placement_touchpoints"):
        enable_rls(table)
    for table in ("start_gates", "placement_touchpoints"):
        enable_org_rls(
            table,
            client_col="client_id",
            vendor_col="vendor_id",
            worker_col="candidate_id",
            inherit_from=InheritRef("placements", "placement_id", ORG),
        )
    enable_org_rls("start_gate_overrides")  # internal only


def downgrade() -> None:
    for table in ("placement_touchpoints", "start_gate_overrides", "start_gates"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
