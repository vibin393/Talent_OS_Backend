# ruff: noqa: E501
"""Epic 49 story 49.5 (FR-248, FR-249): ``aedt_jurisdiction_flags`` (NYC LL144 flag per Demand; the notice period
starts at ``notice_published_at``) and ``aedt_alternative_requests`` (alternative selection process requests; the
Candidate reads its own rows through the worker org branch).

Revision ID: 20261007_313_03_aedt_ll144
Revises: 20261007_313_02_fairness_runs
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_313_03_aedt_ll144"
down_revision: str | tuple[str, ...] | None = "20261007_313_02_fairness_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_{table}_{column}_{target}",
        ondelete=ondelete,
    )


def upgrade() -> None:
    t = "aedt_jurisdiction_flags"
    op.create_table(
        t,
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("jurisdiction", sa.String(10), server_default=sa.text("'nyc'"), nullable=False),
        sa.Column("notice_published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("flagged_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("demand_id", name="pk_aedt_jurisdiction_flags"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_aedt_jurisdiction_flags_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("jurisdiction IN ('nyc')", name=op.f("ck_aedt_jurisdiction_flags_jurisdiction")),
        _fk(t, "demand_id", "demands", "CASCADE"),
        _fk(t, "flagged_by", "users"),
    )
    op.create_index("ix_aedt_jurisdiction_flags_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)

    t = "aedt_alternative_requests"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("status", sa.String(10), server_default=sa.text("'open'"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_aedt_alternative_requests"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_aedt_alternative_requests_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("application_id", name="uq_aedt_alternative_requests_app"),
        sa.CheckConstraint(
            "channel IN ('apply', 'portal')", name=op.f("ck_aedt_alternative_requests_channel")
        ),
        sa.CheckConstraint("status IN ('open', 'handled')", name=op.f("ck_aedt_alternative_requests_status")),
        _fk(t, "application_id", "applications", "CASCADE"),
        _fk(t, "candidate_id", "candidates", "CASCADE"),
        _fk(t, "demand_id", "demands", "CASCADE"),
    )
    op.create_index("ix_aedt_alternative_requests_tenant_id", t, ["tenant_id"])
    op.create_index("ix_aedt_alternative_requests_cand", t, ["candidate_id"])
    op.create_index("ix_aedt_alternative_requests_demand", t, ["demand_id"])
    enable_rls(t)
    enable_org_rls(t, worker_col="candidate_id")


def downgrade() -> None:
    for table in ("aedt_alternative_requests", "aedt_jurisdiction_flags"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
