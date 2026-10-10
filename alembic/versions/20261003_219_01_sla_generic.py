# ruff: noqa: E501
"""Epic 32 (story 19.1): generic SLA clocks -- evaluator state and org columns on the foundation ``sla_clocks``.

* ``sla_clocks`` += ``status`` (healthy | at_risk | breached | met | cancelled, kept by the evaluator beat),
  ``last_notified_status``, ``breached_at`` (a breach is never erased) and the org columns ``org_client_id`` /
  ``org_vendor_id`` (filled from ``scope['client_id' | 'vendor_id']`` at start); the org RLS policy is
  re-created on them so portals see only their own clocks.
* ``sla_targets`` needs no change: the foundation (R14) already has ``subject_type``, ``scope`` (JSONB),
  ``calendar`` and ``is_active``.

Revision ID: 20261003_219_01_sla_generic
Revises: 20261002_200_07
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, enable_org_rls

revision: str = "20261003_219_01_sla_generic"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_workflow_sla",)
depends_on: str | Sequence[str] | None = None

STATUSES = ("healthy", "at_risk", "breached", "met", "cancelled")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.add_column(
        "sla_clocks",
        sa.Column("status", sa.String(length=10), server_default=sa.text("'healthy'"), nullable=False),
    )
    op.add_column("sla_clocks", sa.Column("last_notified_status", sa.String(length=10), nullable=True))
    op.add_column("sla_clocks", sa.Column("breached_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sla_clocks", sa.Column("org_client_id", sa.Uuid(), nullable=True))
    op.add_column("sla_clocks", sa.Column("org_vendor_id", sa.Uuid(), nullable=True))
    op.create_check_constraint(op.f("ck_sla_clocks_status"), "sla_clocks", _in("status", STATUSES))
    op.create_check_constraint(
        op.f("ck_sla_clocks_last_notified_status"),
        "sla_clocks",
        "last_notified_status IS NULL OR " + _in("last_notified_status", STATUSES),
    )
    op.execute(
        "UPDATE sla_clocks SET status = CASE outcome WHEN 'met' THEN 'met' WHEN 'breached' THEN 'breached' "
        "WHEN 'cancelled' THEN 'cancelled' ELSE 'healthy' END, "
        "breached_at = CASE WHEN outcome = 'breached' THEN due_at END"
    )
    op.create_index(
        "ix_sla_clocks_tenant_open",
        "sla_clocks",
        ["tenant_id", "completed_at"],
        postgresql_where=sa.text("completed_at IS NULL"),
    )
    op.create_index(
        "ix_sla_clocks_summary", "sla_clocks", ["tenant_id", "subject_type", "milestone", "started_at"]
    )
    disable_org_rls("sla_clocks")
    enable_org_rls("sla_clocks", client_col="org_client_id", vendor_col="org_vendor_id")


def downgrade() -> None:
    disable_org_rls("sla_clocks")
    enable_org_rls("sla_clocks")
    op.drop_index("ix_sla_clocks_summary", table_name="sla_clocks")
    op.drop_index("ix_sla_clocks_tenant_open", table_name="sla_clocks")
    op.drop_constraint(op.f("ck_sla_clocks_last_notified_status"), "sla_clocks", type_="check")
    op.drop_constraint(op.f("ck_sla_clocks_status"), "sla_clocks", type_="check")
    for column in ("org_vendor_id", "org_client_id", "breached_at", "last_notified_status", "status"):
        op.drop_column("sla_clocks", column)
