"""Epic 25 (Timesheets) 212_03: ``timesheet_adjustment_entries``, ``rate_adjustments`` and
``timesheet_reminders_sent`` (FR-132/133, AD-21, AD-23).

* Adjustment entries follow their (adjustment) timesheet's status and are locked while posted
  (source kind ``adjustment_entry``).
* ``rate_adjustments`` rows never change once written (immutable) and are internal-only.

Revision ID: 20261020_212_03_time_adjust
Revises: 20261020_212_02_time_timesheets
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import (
    InheritRef,
    ParentRef,
    disable_immutability,
    disable_org_rls,
    disable_rls,
    disable_source_lock,
    enable_immutability,
    enable_org_rls,
    enable_rls,
    enable_source_lock,
)

revision: str = "20261020_212_03_time_adjust"
down_revision: str | tuple[str, ...] | None = "20261020_212_02_time_timesheets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG = ("client_id", "vendor_id", "candidate_id")
_CATEGORIES = "category IN ('regular', 'ot', 'double_time', 'holiday', 'leave_paid', 'leave_unpaid')"


def _tfk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_{table}_{column}_{target}",
        ondelete=ondelete,
    )


def _tenant(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
    )


def upgrade() -> None:
    t = "timesheet_adjustment_entries"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("timesheet_id", sa.Uuid(), nullable=False),
        sa.Column("original_timesheet_id", sa.Uuid(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("delta_hours", sa.Numeric(8, 2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(_CATEGORIES, name=f"ck_{t}_category"),
        sa.CheckConstraint("delta_hours <> 0 AND delta_hours BETWEEN -24 AND 24", name=f"ck_{t}_delta"),
        _tenant(t),
        _tfk(t, "timesheet_id", "timesheets", "CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "original_timesheet_id"],
            ["timesheets.tenant_id", "timesheets.id"],
            name="fk_timesheet_adjustment_entries_original_timesheets",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("timesheet_id", "entry_date", "category", name=f"uq_{t}_cell"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index(f"ix_{t}_timesheet_id", t, ["timesheet_id"])
    op.create_index(f"ix_{t}_original", t, ["original_timesheet_id", "entry_date"])

    r = "rate_adjustments"
    op.create_table(
        r,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("ledger", sa.String(10), nullable=False),
        sa.Column("original_source_kind", sa.String(32), nullable=False),
        sa.Column("original_source_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("item_date", sa.Date(), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("quantity", sa.Numeric(8, 2), nullable=False),
        sa.Column("base_rate", sa.Numeric(14, 4), nullable=False),
        sa.Column("new_rate", sa.Numeric(14, 4), nullable=False),
        sa.Column("rate_delta", sa.Numeric(14, 4), nullable=False),
        sa.Column("multiplier", sa.Numeric(14, 4), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("segment_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("ledger IN ('billing', 'payable')", name=f"ck_{r}_ledger"),
        sa.CheckConstraint("rate_delta <> 0", name=f"ck_{r}_rate_delta"),
        _tenant(r),
        _tfk(r, "placement_id", "placements"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{r}"),
        sa.UniqueConstraint(
            "tenant_id",
            "ledger",
            "original_source_kind",
            "original_source_id",
            "segment_id",
            name="uq_rate_adjustments_source_segment",
        ),
    )
    op.create_index(f"ix_{r}_tenant_id", r, ["tenant_id"])
    op.create_index(f"ix_{r}_source", r, ["original_source_kind", "original_source_id"])
    op.create_index(f"ix_{r}_placement_id", r, ["placement_id", "item_date"])

    s = "timesheet_reminders_sent"
    op.create_table(
        s,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("timesheet_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("recipient_kind", sa.String(20), nullable=False),
        sa.Column("recipient_ref", sa.Uuid(), nullable=True),
        sa.Column("sent_by", sa.Uuid(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _tenant(s),
        _tfk(s, "timesheet_id", "timesheets", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{s}"),
    )
    op.create_index(f"ix_{s}_tenant_id", s, ["tenant_id"])
    op.create_index(f"ix_{s}_timesheet_id", s, ["timesheet_id", "sent_at"])

    for table in (t, r, s):
        enable_rls(table)
    enable_org_rls(
        t,
        client_col="client_id",
        vendor_col="vendor_id",
        worker_col="candidate_id",
        inherit_from=InheritRef("timesheets", "timesheet_id", ORG),
    )
    enable_org_rls(r)
    enable_org_rls(s)
    enable_immutability(
        t, status_column=None, parent=ParentRef("timesheets", "timesheet_id", "status", ("draft", "rejected"))
    )
    enable_source_lock(t, source_kind="adjustment_entry")
    enable_immutability(r, status_column=None)
    enable_source_lock(r, source_kind="rate_adjustment")


def downgrade() -> None:
    for table in ("rate_adjustments", "timesheet_adjustment_entries"):
        disable_source_lock(table)
        disable_immutability(table)
    for table in ("timesheet_reminders_sent", "rate_adjustments", "timesheet_adjustment_entries"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
