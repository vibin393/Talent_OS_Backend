"""Epic 25 (Timesheets) 212_02: ``timesheets`` and ``timesheet_entries`` (FR-129/130, AD-21, AD-26).

* Org RLS on both (client / vendor / worker columns); entries inherit them from their timesheet.
* ``timesheet_entries`` are immutable unless the parent is ``draft`` / ``rejected`` (``tos_immutable`` with a
  parent reference; the OT/DT split is written by ``submit`` before the status leaves ``draft``) and while any
  non-released posting of the entry exists in either ledger (``tos_source_lock``, source kind
  ``timesheet_entry``).

Revision ID: 20261020_212_02_time_timesheets
Revises: 20261020_212_01_time_configs
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

revision: str = "20261020_212_02_time_timesheets"
down_revision: str | tuple[str, ...] | None = "20261020_212_01_time_configs"
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


def upgrade() -> None:
    op.create_table(
        "timesheets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(12), server_default=sa.text("'regular'"), nullable=False),
        sa.Column("adjustment_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("original_timesheet_id", sa.Uuid(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("config_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_by_kind", sa.String(20), nullable=True),
        sa.Column("submitted_by_id", sa.Uuid(), nullable=True),
        sa.Column("proxy_reason", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("client_approval_evidence_file_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("rejection_comment", sa.Text(), nullable=True),
        sa.Column("rejected_step", sa.String(100), nullable=True),
        sa.Column("missing_flagged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('regular', 'adjustment')", name="ck_timesheets_kind"),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'in_approval', 'approved', 'rejected')",
            name="ck_timesheets_status",
        ),
        sa.CheckConstraint("period_end >= period_start", name="ck_timesheets_period"),
        sa.CheckConstraint(
            "(kind = 'regular' AND adjustment_seq = 0 AND original_timesheet_id IS NULL) "
            "OR (kind = 'adjustment' AND adjustment_seq > 0 AND original_timesheet_id IS NOT NULL)",
            name="ck_timesheets_adjustment",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_timesheets_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _tfk("timesheets", "placement_id", "placements"),
        _tfk("timesheets", "candidate_id", "candidates"),
        _tfk("timesheets", "client_id", "clients"),
        _tfk("timesheets", "legal_entity_id", "legal_entities"),
        _tfk("timesheets", "original_timesheet_id", "timesheets"),
        sa.ForeignKeyConstraint(
            ["approval_request_id"],
            ["approval_requests.id"],
            name="fk_timesheets_approval_request_id_approval_requests",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["client_approval_evidence_file_id"],
            ["stored_files.id"],
            name="fk_timesheets_client_approval_evidence_file_id_stored_files",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_timesheets"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_timesheets_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_timesheets_tenant_id_human_id"),
        sa.UniqueConstraint(
            "tenant_id", "placement_id", "period_start", "kind", "adjustment_seq", name="uq_timesheets_period"
        ),
    )
    op.create_index("ix_timesheets_tenant_id", "timesheets", ["tenant_id"])
    op.create_index("ix_timesheets_placement_id", "timesheets", ["placement_id", "period_start"])
    op.create_index("ix_timesheets_candidate_id", "timesheets", ["candidate_id"])
    op.create_index("ix_timesheets_client_id", "timesheets", ["client_id"])
    op.create_index("ix_timesheets_legal_entity_id", "timesheets", ["legal_entity_id", "period_start"])
    op.create_index("ix_timesheets_status", "timesheets", ["status"])

    op.create_table(
        "timesheet_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("timesheet_id", sa.Uuid(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("hours", sa.Numeric(8, 2), nullable=False),
        sa.Column("entered_hours", sa.Numeric(8, 2), nullable=True),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("break_minutes", sa.Integer(), nullable=True),
        sa.Column("leave_type_id", sa.Uuid(), nullable=True),
        sa.Column("holiday_id", sa.Uuid(), nullable=True),
        sa.Column("prefilled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("prefill_deletable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_proxy", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(_CATEGORIES, name="ck_timesheet_entries_category"),
        sa.CheckConstraint("hours >= 0 AND hours <= 24", name="ck_timesheet_entries_hours"),
        sa.CheckConstraint(
            "entered_hours IS NULL OR (entered_hours >= 0 AND entered_hours <= 24)",
            name="ck_timesheet_entries_entered",
        ),
        sa.CheckConstraint(
            "break_minutes IS NULL OR break_minutes >= 0", name="ck_timesheet_entries_break_minutes"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_timesheet_entries_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _tfk("timesheet_entries", "timesheet_id", "timesheets", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_timesheet_entries"),
        sa.UniqueConstraint("timesheet_id", "entry_date", "category", name="uq_timesheet_entries_cell"),
    )
    op.create_index("ix_timesheet_entries_tenant_id", "timesheet_entries", ["tenant_id"])
    op.create_index("ix_timesheet_entries_timesheet_id", "timesheet_entries", ["timesheet_id"])
    op.create_index("ix_timesheet_entries_entry_date", "timesheet_entries", ["entry_date"])

    for table in ("timesheets", "timesheet_entries"):
        enable_rls(table)
    enable_org_rls("timesheets", client_col="client_id", vendor_col="vendor_id", worker_col="candidate_id")
    enable_org_rls(
        "timesheet_entries",
        client_col="client_id",
        vendor_col="vendor_id",
        worker_col="candidate_id",
        inherit_from=InheritRef("timesheets", "timesheet_id", ORG),
    )
    enable_immutability(
        "timesheet_entries",
        status_column=None,
        parent=ParentRef("timesheets", "timesheet_id", "status", ("draft", "rejected")),
    )
    enable_source_lock("timesheet_entries", source_kind="timesheet_entry")


def downgrade() -> None:
    disable_source_lock("timesheet_entries")
    disable_immutability("timesheet_entries")
    for table in ("timesheet_entries", "timesheets"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
