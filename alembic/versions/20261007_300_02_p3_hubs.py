# ruff: noqa: E501
"""Phase 3 foundation (Epic 36, story 36.3): the six contract-layer hub tables (spec 0.2, AD-39).

``talent_pools`` (38), ``assessments`` / ``assessment_invitations`` (40), ``projects`` / ``project_roles`` (45),
``workforce_plan_lines`` (44). Owners ALTER them only in their own branch. FKs to tables that do not exist yet
(``talent_pools.segment_id``, ``assessment_invitations.assessment_version_id``,
``workforce_plan_lines.workforce_plan_id``) are plain UUID columns; the owner adds the FK in its first revision.

Org RLS: ``talent_pools`` internal only (policy without columns); ``assessment_invitations`` worker scope on
``candidate_id``; ``projects`` / ``workforce_plan_lines`` client scope on ``client_id``; ``project_roles`` client
scope with ``client_id`` inherited from the project (``tos_inherit_org``). ``assessments`` tenant RLS only.

Revision ID: 20261007_300_02_p3_hubs
Revises: 20261007_300_01_p3_checks
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_300_02_p3_hubs"
down_revision: str | tuple[str, ...] | None = "20261007_300_01_p3_checks"
branch_labels: str | Sequence[str] | None = None
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
    # ---------------------------------------------------------------- talent_pools (Epic 38)
    t = "talent_pools"
    op.create_table(
        t,
        *_base(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("segment_id", sa.Uuid(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("visibility", sa.String(10), server_default=sa.text("'private'"), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        _fk(t, "owner_id", "users"),
        sa.CheckConstraint("kind IN ('static', 'dynamic')", name=op.f("ck_talent_pools_kind")),
        sa.CheckConstraint(
            "visibility IN ('private', 'team', 'tenant')", name=op.f("ck_talent_pools_visibility")
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name=op.f("ck_talent_pools_status")),
        sa.CheckConstraint(
            "kind = 'dynamic' OR segment_id IS NULL", name=op.f("ck_talent_pools_segment_dynamic")
        ),
    )
    _ix(t, "tenant_id")
    _ix(t, "owner_id")
    _ix(t, "segment_id")
    enable_rls(t)
    enable_org_rls(t)  # no columns: internal sessions only

    # ---------------------------------------------------------------- assessments (Epic 40)
    t = "assessments"
    op.create_table(
        t,
        *_base(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("delivery", sa.String(20), server_default=sa.text("'builtin'"), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        _fk(t, "owner_id", "users"),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name=op.f("ck_assessments_status")
        ),
        sa.CheckConstraint("delivery IN ('builtin', 'provider')", name=op.f("ck_assessments_delivery")),
    )
    _ix(t, "tenant_id")
    _ix(t, "owner_id")
    enable_rls(t)

    t = "assessment_invitations"
    op.create_table(
        t,
        *_base(t),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_version_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("context_kind", sa.String(20), nullable=False),
        sa.Column("context_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'invited'"), nullable=False),
        sa.Column("gating", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("result", sa.String(10), nullable=True),
        sa.Column("score", sa.Numeric(6, 2), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invited_by_kind", sa.String(20), nullable=False),
        sa.Column("invited_by_id", sa.Uuid(), nullable=True),
        sa.Column("provider_ref", sa.String(200), nullable=True),
        _fk(t, "assessment_id", "assessments"),
        _fk(t, "candidate_id", "candidates"),
        _fk(t, "application_id", "applications"),
        sa.CheckConstraint(
            "context_kind IN ('application', 'campus_drive', 'event', 'direct')",
            name=op.f("ck_assessment_invitations_context_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('invited', 'started', 'expired', 'completed', 'reviewed', 'passed', 'failed', 'cancelled')",
            name=op.f("ck_assessment_invitations_status"),
        ),
        sa.CheckConstraint(
            "result IS NULL OR result IN ('pass', 'fail')", name=op.f("ck_assessment_invitations_result")
        ),
        sa.CheckConstraint(
            "score IS NULL OR score BETWEEN 0 AND 100", name=op.f("ck_assessment_invitations_score")
        ),
        sa.CheckConstraint(
            "(status = 'passed' AND result IS NOT DISTINCT FROM 'pass') "
            "OR (status = 'failed' AND result IS NOT DISTINCT FROM 'fail') "
            "OR (status NOT IN ('passed', 'failed') AND result IS NULL)",
            name=op.f("ck_assessment_invitations_status_result"),
        ),
        sa.CheckConstraint(
            "context_kind <> 'application' OR application_id IS NOT NULL",
            name=op.f("ck_assessment_invitations_application_ctx"),
        ),
    )
    _ix(t, "tenant_id")
    _ix(t, "assessment_id")
    _ix(t, "candidate_id")
    _ix(t, "application_id")
    op.create_index(
        "uq_assessment_invitations_open",
        t,
        ["tenant_id", "assessment_id", "candidate_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('invited', 'started')"),
    )
    enable_rls(t)
    enable_org_rls(t, worker_col="candidate_id")

    # ---------------------------------------------------------------- projects (Epic 45)
    t = "projects"
    op.create_table(
        t,
        *_base(t),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_projects_tenant_id_human_id"),
        _fk(t, "client_id", "clients"),
        _fk(t, "contract_id", "contracts"),
        _fk(t, "owner_id", "users"),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'on_hold', 'completed', 'cancelled')",
            name=op.f("ck_projects_status"),
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date", name=op.f("ck_projects_dates")
        ),
    )
    _ix(t, "tenant_id")
    _ix(t, "client_id")
    _ix(t, "contract_id")
    _ix(t, "owner_id")
    enable_rls(t)
    enable_org_rls(t, client_col="client_id")

    t = "project_roles"
    op.create_table(
        t,
        *_base(t),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("headcount", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'open'"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        _fk(t, "project_id", "projects"),
        sa.CheckConstraint(
            "status IN ('open', 'filled', 'closed', 'cancelled')", name=op.f("ck_project_roles_status")
        ),
        sa.CheckConstraint("headcount >= 1", name=op.f("ck_project_roles_headcount")),
        sa.CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name=op.f("ck_project_roles_dates"),
        ),
    )
    _ix(t, "tenant_id")
    _ix(t, "project_id")
    _ix(t, "client_id")
    enable_rls(t)
    enable_org_rls(
        t, client_col="client_id", inherit_from=InheritRef("projects", "project_id", ("client_id",))
    )

    # ---------------------------------------------------------------- workforce_plan_lines (Epic 44)
    t = "workforce_plan_lines"
    op.create_table(
        t,
        *_base(t),
        sa.Column("workforce_plan_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("role_title", sa.String(200), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("headcount", sa.Integer(), nullable=False),
        sa.Column("project_role_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'forecast'"), nullable=False),
        _fk(t, "client_id", "clients"),
        _fk(t, "project_role_id", "project_roles"),
        sa.CheckConstraint(
            "status IN ('forecast', 'budgeted', 'approved', 'requisitioned', 'cancelled')",
            name=op.f("ck_workforce_plan_lines_status"),
        ),
        sa.CheckConstraint("headcount >= 1", name=op.f("ck_workforce_plan_lines_headcount")),
        sa.CheckConstraint("period_end >= period_start", name=op.f("ck_workforce_plan_lines_period")),
    )
    _ix(t, "tenant_id")
    _ix(t, "workforce_plan_id")
    _ix(t, "client_id")
    _ix(t, "project_role_id")
    enable_rls(t)
    enable_org_rls(t, client_col="client_id")


def downgrade() -> None:
    for t in (
        "workforce_plan_lines",
        "project_roles",
        "projects",
        "assessment_invitations",
        "assessments",
        "talent_pools",
    ):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
