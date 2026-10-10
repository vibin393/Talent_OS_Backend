# ruff: noqa: E501
"""Epic 45 (SOW & project staffing, FR-227/228/229): hub detail columns and ``project_assignments``.

``projects`` gains ``legal_entity_id`` / ``currency`` (from the SOW), ``business_unit_id``, ``description``,
``risk_note``, ``archived_at``; ``project_roles`` gains ``skills`` (JSONB), ``bill_rate`` / ``bill_rate_unit`` and
``notes``. Foundation keys, org columns and status sets are untouched. ``project_assignments`` inherits
``client_id`` from ``projects`` (``tos_inherit_org``) with client org RLS; one active assignment per Placement.

Revision ID: 20261007_309_01_projects
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_309_01_projects"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_projects",)
depends_on: str | Sequence[str] | None = None


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


def _fk(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_{table}_{column}_{target}",
        ondelete="RESTRICT",
    )


def _ix(table: str, *columns: str) -> None:
    op.create_index(f"ix_{table}_{'_'.join(columns)}", table, list(columns))


def upgrade() -> None:
    # ---------------------------------------------------------------- projects detail columns
    op.add_column("projects", sa.Column("legal_entity_id", sa.Uuid(), nullable=True))
    op.add_column("projects", sa.Column("currency", sa.CHAR(3), nullable=True))
    op.add_column("projects", sa.Column("business_unit_id", sa.Uuid(), nullable=True))
    op.add_column("projects", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("risk_note", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_projects_legal_entity_id_legal_entities",
        "projects",
        "legal_entities",
        ["tenant_id", "legal_entity_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_projects_business_unit_id_business_units",
        "projects",
        "business_units",
        ["tenant_id", "business_unit_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    _ix("projects", "business_unit_id")

    # ---------------------------------------------------------------- project_roles detail columns
    op.add_column(
        "project_roles",
        sa.Column("skills", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column("project_roles", sa.Column("bill_rate", sa.Numeric(14, 4), nullable=True))
    op.add_column("project_roles", sa.Column("bill_rate_unit", sa.String(10), nullable=True))
    op.add_column("project_roles", sa.Column("notes", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_project_roles_rate_unit"),
        "project_roles",
        "bill_rate_unit IS NULL OR bill_rate_unit IN ('hour', 'day', 'month', 'fixed')",
    )
    op.create_check_constraint(
        op.f("ck_project_roles_bill_rate"), "project_roles", "bill_rate IS NULL OR bill_rate >= 0"
    )

    # ---------------------------------------------------------------- project_assignments
    t = "project_assignments"
    op.create_table(
        t,
        *_base(t),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("project_role_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("allocation_pct", sa.Numeric(5, 2), server_default=sa.text("100"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("ended_reason", sa.String(500), nullable=True),
        _fk(t, "project_id", "projects"),
        _fk(t, "project_role_id", "project_roles"),
        _fk(t, "placement_id", "placements"),
        sa.CheckConstraint("status IN ('active', 'ended')", name=op.f("ck_project_assignments_status")),
        sa.CheckConstraint(
            "allocation_pct > 0 AND allocation_pct <= 100", name=op.f("ck_project_assignments_allocation")
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date", name=op.f("ck_project_assignments_dates")
        ),
    )
    _ix(t, "tenant_id")
    _ix(t, "project_id")
    _ix(t, "project_role_id")
    _ix(t, "placement_id")
    _ix(t, "client_id")
    op.create_index(
        "uq_project_assignments_active_placement",
        t,
        ["placement_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    enable_rls(t)
    enable_org_rls(
        t, client_col="client_id", inherit_from=InheritRef("projects", "project_id", ("client_id",))
    )


def downgrade() -> None:
    t = "project_assignments"
    disable_org_rls(t)
    disable_rls(t)
    op.drop_table(t)

    op.drop_constraint(op.f("ck_project_roles_bill_rate"), "project_roles", type_="check")
    op.drop_constraint(op.f("ck_project_roles_rate_unit"), "project_roles", type_="check")
    for col in ("notes", "bill_rate_unit", "bill_rate", "skills"):
        op.drop_column("project_roles", col)

    op.drop_index("ix_projects_business_unit_id", table_name="projects")
    op.drop_constraint("fk_projects_business_unit_id_business_units", "projects", type_="foreignkey")
    op.drop_constraint("fk_projects_legal_entity_id_legal_entities", "projects", type_="foreignkey")
    for col in ("archived_at", "risk_note", "description", "business_unit_id", "currency", "legal_entity_id"):
        op.drop_column("projects", col)
