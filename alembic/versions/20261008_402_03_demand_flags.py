# ruff: noqa: E501
"""Epic 55 stories 55.4 / 55.5 (FR-283, FR-284): ``demand_flags`` -- possible-duplicate and SLA-risk flags. A flag
never blocks or merges; a person resolves it.

Tenant RLS + internal-only org RLS.

Revision ID: 20261008_402_03_demand_flags
Revises: 20261008_402_02_field_sources
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_402_03_demand_flags"
down_revision: str | tuple[str, ...] | None = "20261008_402_02_field_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FLAG_KINDS = ("possible_duplicate", "sla_risk")
FLAG_STATUSES = ("open", "linked_duplicate", "distinct", "dismissed", "resolved", "expired")
FLAG_BASES = ("prediction", "pipeline_only", "late")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def upgrade() -> None:
    t = "demand_flags"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("related_demand_id", sa.Uuid(), nullable=True),
        sa.Column("score", sa.Numeric(4, 3), nullable=True),
        sa.Column("reasons", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("summary", sa.String(500), nullable=True),
        sa.Column("milestone", sa.String(30), nullable=True),
        sa.Column("basis", sa.String(20), nullable=True),
        sa.Column("p_late", sa.Numeric(4, 3), nullable=True),
        sa.Column("next_steps", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'open'"), nullable=False),
        sa.Column("raised_by_kind", sa.String(20), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.String(500), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _fk("demand_id", "demands", "fk_demand_flags_demand_id_demands", "CASCADE"),
        _fk("related_demand_id", "demands", "fk_demand_flags_related_demand_id_demands", "CASCADE"),
        _fk("resolved_by", "users", "fk_demand_flags_resolved_by_users"),
        sa.CheckConstraint(_in("kind", FLAG_KINDS), name=op.f("ck_demand_flags_kind")),
        sa.CheckConstraint(_in("status", FLAG_STATUSES), name=op.f("ck_demand_flags_status")),
        sa.CheckConstraint(
            f"basis IS NULL OR {_in('basis', FLAG_BASES)}", name=op.f("ck_demand_flags_basis")
        ),
        sa.CheckConstraint(
            "kind <> 'possible_duplicate' OR related_demand_id IS NOT NULL",
            name=op.f("ck_demand_flags_related"),
        ),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index(
        "uq_demand_flags_open_dedupe", t, ["tenant_id", "dedupe_key"], unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )  # fmt: skip
    op.create_index("ix_demand_flags_kind_status", t, ["tenant_id", "kind", "status"])
    op.create_index("ix_demand_flags_related_demand_id", t, ["related_demand_id"])
    op.create_index("ix_demand_flags_demand_id", t, ["demand_id"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    disable_org_rls("demand_flags")
    disable_rls("demand_flags")
    op.drop_table("demand_flags")
