# ruff: noqa: E501
"""Epic 41 story 41.1 / 41.2 (FR-209, FR-210, AD-50): internal mobility settings, employees, opening rules, the
board projection and the internal application overlay.

* ``mobility_settings``, ``mobility_profiles``, ``mobility_employee_links``, ``internal_opening_rules``: tenant RLS +
  internal-only org RLS.
* ``internal_openings``: tenant RLS + a custom RESTRICTIVE ``org_isolation`` policy -- internal and worker sessions
  read it (any Worker of the tenant may browse the board; the table holds no client or compensation data); client /
  vendor sessions see no rows.
* ``mobility_applications``: tenant RLS + worker org RLS on ``candidate_id``.

Revision ID: 20261007_305_01_mobility
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_305_01_mobility"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_mobility",)
depends_on: str | Sequence[str] | None = None

DISCLOSURE = "Your current manager will be asked to approve this move when your application reaches the approval step."
APP_STATUSES = ("applied", "manager_approval", "approved", "manager_rejected", "withdrawn", "transfer_confirmed")
OPENINGS_POLICY = "current_setting('app.org_kind', true) IN ('internal', 'worker')"


def _in(col: str, values: Sequence[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant_col() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column("tenant_id", sa.Uuid(), nullable=False)


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
    )


def _id(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        _tenant_col(),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        _tenant_fk(table),
    ]


def _fk(name: str, col: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def upgrade() -> None:
    t = "mobility_settings"
    op.create_table(
        t,
        _tenant_col(),
        sa.Column("board_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("worker_board_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("screen_before_manager", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "fallback_approver_role_key", sa.String(60), server_default=sa.text("'recruitment_manager'"), nullable=False
        ),
        sa.Column("min_tenure_default_months", sa.SmallInteger(), nullable=True),
        sa.Column("disclosure_text", sa.Text(), server_default=sa.text(f"'{DISCLOSURE}'"), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("tenant_id", name=f"pk_{t}"),
        _tenant_fk(t),
        sa.CheckConstraint(
            "min_tenure_default_months IS NULL OR min_tenure_default_months BETWEEN 0 AND 600",
            name=op.f("ck_mobility_settings_tenure"),
        ),
    )
    op.create_index("ix_mobility_settings_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)

    t = "mobility_profiles"
    op.create_table(
        t,
        *_id(t),
        sa.Column("subject_kind", sa.String(10), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("grade", sa.String(40), nullable=True),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "subject_kind", "subject_id", name="uq_mobility_profiles_subject"),
        sa.CheckConstraint("subject_kind IN ('user', 'worker')", name=op.f("ck_mobility_profiles_subject_kind")),
        _fk("fk_mobility_profiles_updated_by_users", "updated_by", "users"),
    )
    op.create_index("ix_mobility_profiles_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)

    t = "mobility_employee_links"
    op.create_table(
        t,
        *_id(t),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_mobility_employee_links_user"),
        sa.UniqueConstraint("tenant_id", "candidate_id", name="uq_mobility_employee_links_cand"),
        _fk("fk_mobility_employee_links_user_id", "user_id", "users"),
        _fk("fk_mobility_employee_links_candidate_id", "candidate_id", "candidates"),
    )
    op.create_index("ix_mobility_employee_links_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)

    t = "internal_opening_rules"
    op.create_table(
        t,
        _tenant_col(),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("min_tenure_months", sa.SmallInteger(), nullable=True),
        sa.Column("grades", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("locations", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("business_unit_ids", postgresql.ARRAY(sa.Uuid()), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("demand_id", name=f"pk_{t}"),
        _tenant_fk(t),
        _fk("fk_internal_opening_rules_demand_id", "demand_id", "demands", "CASCADE"),
        sa.CheckConstraint(
            "min_tenure_months IS NULL OR min_tenure_months BETWEEN 0 AND 600",
            name=op.f("ck_internal_opening_rules_tenure"),
        ),
    )
    op.create_index("ix_internal_opening_rules_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)

    t = "internal_openings"
    op.create_table(
        t,
        _tenant_col(),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("location_text", sa.String(300), nullable=True),
        sa.Column("work_mode", sa.String(10), nullable=True),
        sa.Column("employment_type", sa.String(20), nullable=True),
        sa.Column("summary_html", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("skills", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("visibility", sa.String(10), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'open'"), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("demand_id", name=f"pk_{t}"),
        _tenant_fk(t),
        _fk("fk_internal_openings_demand_id", "demand_id", "demands", "CASCADE"),
        sa.CheckConstraint("visibility IN ('internal', 'both')", name=op.f("ck_internal_openings_visibility")),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_internal_openings_status")),
    )
    op.create_index("ix_internal_openings_tenant_id", t, ["tenant_id"])
    op.create_index("ix_internal_openings_status", t, ["tenant_id", "status"])
    enable_rls(t)
    op.execute(f"DROP POLICY IF EXISTS org_isolation ON {t}")
    op.execute(
        f"CREATE POLICY org_isolation ON {t} AS RESTRICTIVE FOR ALL USING ({OPENINGS_POLICY}) WITH CHECK ({OPENINGS_POLICY})"
    )

    t = "mobility_applications"
    op.create_table(
        t,
        *_id(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("employee_kind", sa.String(10), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("current_placement_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'applied'"), nullable=False),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("manager_user_id", sa.Uuid(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("cover_note", sa.Text(), nullable=True),
        sa.Column("disclosed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("application_id", name="uq_mobility_applications_application_id"),
        sa.CheckConstraint(
            "employee_kind IN ('user', 'worker')", name=op.f("ck_mobility_applications_employee_kind")
        ),
        sa.CheckConstraint(_in("status", APP_STATUSES), name=op.f("ck_mobility_applications_status")),
        sa.CheckConstraint(
            "employee_kind = 'worker' OR user_id IS NOT NULL", name=op.f("ck_mobility_applications_user_link")
        ),
        _fk("fk_mobility_applications_application_id", "application_id", "applications"),
        _fk("fk_mobility_applications_demand_id", "demand_id", "demands"),
        _fk("fk_mobility_applications_candidate_id", "candidate_id", "candidates"),
        _fk("fk_mobility_applications_user_id", "user_id", "users"),
        _fk("fk_mobility_applications_placement_id", "current_placement_id", "placements"),
        _fk("fk_mobility_applications_manager_user_id", "manager_user_id", "users"),
    )
    for name, cols in (
        ("ix_mobility_applications_tenant_id", ["tenant_id"]),
        ("ix_mobility_applications_demand_id", ["demand_id"]),
        ("ix_mobility_applications_candidate_id", ["candidate_id"]),
        ("ix_mobility_applications_user_id", ["user_id"]),
    ):
        op.create_index(name, t, cols)
    enable_rls(t)
    enable_org_rls(t, worker_col="candidate_id")


def downgrade() -> None:
    for t in (
        "mobility_applications",
        "internal_openings",
        "internal_opening_rules",
        "mobility_employee_links",
        "mobility_profiles",
        "mobility_settings",
    ):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
