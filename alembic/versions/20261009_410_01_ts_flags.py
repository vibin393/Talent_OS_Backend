# ruff: noqa: E501
"""Epic 63 (story 63.2, FR-312): deterministic timesheet anomaly flags.

* ``timesheet_flags`` (audited, ``explanation`` excluded) -- one rule hit per evaluated timesheet version.

Internal only: tenant RLS + internal-only org RLS. No ALTER of any P1-P3 table.

Revision ID: 20261009_410_01_ts_flags
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261009_410_01_ts_flags"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_finance_agents",)
depends_on: str | Sequence[str] | None = None

T = "timesheet_flags"
RULES = (
    "hours_on_leave", "holiday_work", "weekend_work", "daily_hours_vs_schedule", "weekly_overtime_vs_history",
    "peer_outlier", "po_hours_cap", "zero_hours_period", "duplicate_entry_pattern",
)  # fmt: skip


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        T,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("timesheet_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("rule", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("entry_dates", postgresql.ARRAY(sa.Date()), server_default=sa.text("'{}'::date[]"), nullable=False),
        sa.Column("facts", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("group_key", sa.String(40), nullable=True),
        sa.Column("explanation", sa.String(600), nullable=True),
        sa.Column("explanation_source", sa.String(10), server_default=sa.text("'rules'"), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'open'"), nullable=False),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(500), nullable=True),
        sa.Column("timesheet_version", sa.Integer(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{T}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{T}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "timesheet_id", "timesheet_version", "rule", name="uq_timesheet_flags_rule"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{T}_tenant_id_tenants", ondelete="RESTRICT"),
        _fk("timesheet_id", "timesheets", "fk_timesheet_flags_timesheet_id", "CASCADE"),
        _fk("placement_id", "placements", "fk_timesheet_flags_placement_id"),
        _fk("candidate_id", "candidates", "fk_timesheet_flags_candidate_id"),
        sa.CheckConstraint(_in("rule", RULES), name=op.f("ck_timesheet_flags_rule")),
        sa.CheckConstraint(_in("severity", ("info", "warning")), name=op.f("ck_timesheet_flags_severity")),
        sa.CheckConstraint(_in("explanation_source", ("rules", "ai")), name=op.f("ck_timesheet_flags_source")),
        sa.CheckConstraint(
            _in("status", ("open", "reviewed", "dismissed", "superseded")), name=op.f("ck_timesheet_flags_status")
        ),
    )  # fmt: skip
    op.create_index("ix_timesheet_flags_tenant_id", T, ["tenant_id"])
    op.create_index("ix_timesheet_flags_timesheet_id", T, ["timesheet_id"])
    op.create_index("ix_timesheet_flags_tenant_status", T, ["tenant_id", "status"])
    op.create_index("ix_timesheet_flags_candidate_id", T, ["candidate_id"])
    enable_rls(T)
    enable_org_rls(T)


def downgrade() -> None:
    disable_org_rls(T)
    disable_rls(T)
    op.drop_table(T)
