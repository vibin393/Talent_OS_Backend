# ruff: noqa: E501
"""Epic 44 (workforce planning, FR-222/223/226): plans, the hub line columns, revisions and settings.

* ``workforce_plans`` -- client org RLS on ``client_id`` (NULL = internal only); period ``< 24`` months.
* Hub ``workforce_plan_lines`` (Epic 36) ALTER: FK ``workforce_plan_id -> workforce_plans`` (ON DELETE CASCADE),
  the line detail columns (revision, lineage, skills, type, location, work mode, budget / bill rate as
  NUMERIC(18,2), rate unit, change kind, supersedes, notes, client comment), the planned-start-month CHECK and
  the ``tos_inherit_org`` trigger copying ``client_id`` from the plan. The FK and the start-month CHECK are added
  ``NOT VALID`` so databases holding Epic 36 contract-test rows (random plan ids) still migrate; every new or
  updated row is checked.
* ``workforce_plan_revisions`` and ``workforce_settings`` -- internal only.

Revision ID: 20261007_308_01_wf_plans
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_308_01_wf_plans"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_workforce",)
depends_on: str | Sequence[str] | None = None

LINES = "workforce_plan_lines"


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(
    table: str, column: str, target: str, name: str | None = None, ondelete: str = "RESTRICT"
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=name or f"fk_{table}_{column}_{target}",
        ondelete=ondelete,
    )


def _ix(table: str, *columns: str) -> None:
    op.create_index(f"ix_{table}_{'_'.join(columns)}", table, list(columns))


def upgrade() -> None:
    # ---------------------------------------------------------------- workforce_plans
    t = "workforce_plans"
    op.create_table(
        t,
        *_base(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("fiscal_label", sa.String(20), nullable=True),
        sa.Column("business_unit_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("origin", sa.String(20), server_default=sa.text("'internal'"), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_by_client_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_revision", sa.SmallInteger(), nullable=True),
        sa.Column("current_revision", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("picked_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        _fk(t, "business_unit_id", "business_units"),
        _fk(t, "client_id", "clients"),
        _fk(t, "project_id", "projects"),
        _fk(t, "owner_id", "users"),
        _fk(
            t,
            "submitted_by_client_user_id",
            "client_users",
            "fk_workforce_plans_submitted_by_client_user_id_cu",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'budgeted', 'pending_approval', 'approved', 'rejected', 'revision_draft')",
            name=op.f("ck_workforce_plans_status"),
        ),
        sa.CheckConstraint(
            "origin IN ('internal', 'client_forecast')", name=op.f("ck_workforce_plans_origin")
        ),
        sa.CheckConstraint(
            "period_end > period_start AND period_end < (period_start + INTERVAL '24 months')::date",
            name=op.f("ck_workforce_plans_period"),
        ),
        sa.CheckConstraint("current_revision >= 0", name=op.f("ck_workforce_plans_revision")),
    )
    _ix(t, "tenant_id")
    _ix(t, "client_id")
    _ix(t, "owner_id")
    _ix(t, "business_unit_id")
    _ix(t, "project_id")
    enable_rls(t)
    enable_org_rls(t, client_col="client_id")

    # ---------------------------------------------------------------- hub workforce_plan_lines (ALTER)
    op.add_column(
        LINES, sa.Column("revision", sa.SmallInteger(), server_default=sa.text("0"), nullable=False)
    )
    op.add_column(LINES, sa.Column("lineage_id", sa.Uuid(), nullable=True))
    op.add_column(
        LINES, sa.Column("skills", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False)
    )
    op.add_column(
        LINES, sa.Column("line_type", sa.String(12), server_default=sa.text("'new'"), nullable=False)
    )
    op.add_column(LINES, sa.Column("location_text", sa.String(300), nullable=True))
    op.add_column(LINES, sa.Column("work_mode", sa.String(10), nullable=True))
    op.add_column(LINES, sa.Column("budget_amount", sa.Numeric(18, 2), nullable=True))
    op.add_column(LINES, sa.Column("bill_rate_amount", sa.Numeric(18, 2), nullable=True))
    op.add_column(LINES, sa.Column("rate_unit", sa.String(10), nullable=True))
    op.add_column(
        LINES, sa.Column("change_kind", sa.String(10), server_default=sa.text("'added'"), nullable=False)
    )
    op.add_column(LINES, sa.Column("supersedes_line_id", sa.Uuid(), nullable=True))
    op.add_column(LINES, sa.Column("notes", sa.Text(), nullable=True))
    op.add_column(LINES, sa.Column("client_comment", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_workforce_plan_lines_line_type"),
        LINES,
        "line_type IN ('new', 'replacement', 'project', 'recurring')",
    )
    op.create_check_constraint(
        op.f("ck_workforce_plan_lines_work_mode"),
        LINES,
        "work_mode IS NULL OR work_mode IN ('onsite', 'remote', 'hybrid')",
    )
    op.create_check_constraint(
        op.f("ck_workforce_plan_lines_rate_unit"),
        LINES,
        "rate_unit IS NULL OR rate_unit IN ('hour', 'day', 'month', 'annual')",
    )
    op.create_check_constraint(
        op.f("ck_workforce_plan_lines_change_kind"),
        LINES,
        "change_kind IN ('unchanged', 'added', 'modified', 'removed')",
    )
    op.create_check_constraint(
        op.f("ck_workforce_plan_lines_money"),
        LINES,
        "(budget_amount IS NULL OR budget_amount >= 0) AND (bill_rate_amount IS NULL OR bill_rate_amount >= 0)",
    )
    # NOT VALID: Epic 36 contract-test rows may exist with any start date / a random plan id.
    op.execute(
        "ALTER TABLE workforce_plan_lines ADD CONSTRAINT ck_workforce_plan_lines_start_month "
        "CHECK (EXTRACT(DAY FROM period_start) = 1) NOT VALID"
    )
    op.execute(
        "ALTER TABLE workforce_plan_lines ADD CONSTRAINT fk_workforce_plan_lines_workforce_plan_id_plans "
        "FOREIGN KEY (tenant_id, workforce_plan_id) REFERENCES workforce_plans (tenant_id, id) "
        "ON DELETE CASCADE NOT VALID"
    )
    _ix(LINES, "lineage_id")
    op.create_index("ix_workforce_plan_lines_plan_revision", LINES, ["workforce_plan_id", "revision"])
    # Lines always carry the plan's client (org inheritance on insert / plan change).
    enable_org_rls(
        LINES,
        client_col="client_id",
        inherit_from=InheritRef("workforce_plans", "workforce_plan_id", ("client_id",)),
    )

    # ---------------------------------------------------------------- workforce_plan_revisions
    t = "workforce_plan_revisions"
    op.create_table(
        t,
        *_base(t),
        sa.Column("workforce_plan_id", sa.Uuid(), nullable=False),
        sa.Column("revision_no", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("diff", postgresql.JSONB(), nullable=True),
        _fk(
            t, "workforce_plan_id", "workforce_plans", "fk_workforce_plan_revisions_plan_id_plans", "CASCADE"
        ),
        sa.UniqueConstraint("workforce_plan_id", "revision_no", name="uq_workforce_plan_revisions_plan_no"),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', 'rejected', 'discarded')",
            name=op.f("ck_workforce_plan_revisions_status"),
        ),
    )
    _ix(t, "tenant_id")
    _ix(t, "workforce_plan_id")
    enable_rls(t)
    enable_org_rls(t)

    # ---------------------------------------------------------------- workforce_settings
    t = "workforce_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "standard_hours_per_month", sa.Numeric(6, 2), server_default=sa.text("168"), nullable=False
        ),
        sa.Column("standard_days_per_month", sa.Numeric(5, 2), server_default=sa.text("21"), nullable=False),
        sa.Column("max_headcount_per_line", sa.Integer(), server_default=sa.text("500"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_workforce_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_workforce_settings_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "standard_hours_per_month > 0 AND standard_hours_per_month <= 744",
            name=op.f("ck_workforce_settings_hours"),
        ),
        sa.CheckConstraint(
            "standard_days_per_month > 0 AND standard_days_per_month <= 31",
            name=op.f("ck_workforce_settings_days"),
        ),
        sa.CheckConstraint(
            "max_headcount_per_line BETWEEN 1 AND 500", name=op.f("ck_workforce_settings_max_headcount")
        ),
    )
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    for t in ("workforce_settings", "workforce_plan_revisions"):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)

    # Restore the hub's own org policy (no inheritance trigger).
    enable_org_rls(LINES, client_col="client_id")
    op.drop_index("ix_workforce_plan_lines_plan_revision", table_name=LINES)
    op.drop_index("ix_workforce_plan_lines_lineage_id", table_name=LINES)
    op.drop_constraint("fk_workforce_plan_lines_workforce_plan_id_plans", LINES, type_="foreignkey")
    op.drop_constraint(op.f("ck_workforce_plan_lines_start_month"), LINES, type_="check")
    for name in ("money", "change_kind", "rate_unit", "work_mode", "line_type"):
        op.drop_constraint(op.f(f"ck_workforce_plan_lines_{name}"), LINES, type_="check")
    for col in (
        "client_comment",
        "notes",
        "supersedes_line_id",
        "change_kind",
        "rate_unit",
        "bill_rate_amount",
        "budget_amount",
        "work_mode",
        "location_text",
        "line_type",
        "skills",
        "lineage_id",
        "revision",
    ):
        op.drop_column(LINES, col)

    t = "workforce_plans"
    disable_org_rls(t)
    disable_rls(t)
    op.drop_table(t)
