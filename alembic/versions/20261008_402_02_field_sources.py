# ruff: noqa: E501
"""Epic 55 stories 55.2 / 55.3 (FR-281, FR-282): ``demand_field_sources`` (verbatim source quotes of AI-extracted
demand fields) and ``demand_suggestions`` (skill / new-skill / contract / rate-card suggestions).

Tenant RLS + internal-only org RLS.

Revision ID: 20261008_402_02_field_sources
Revises: 20261008_402_01_demand_intake
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_402_02_field_sources"
down_revision: str | tuple[str, ...] | None = "20261008_402_01_demand_intake"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("demand_field_sources", "demand_suggestions")
SUGGESTION_KINDS = ("skill", "new_skill", "contract", "rate_card")
SUGGESTION_STATUSES = ("pending", "accepted", "dismissed", "superseded")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


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


def upgrade() -> None:
    t = "demand_field_sources"
    op.create_table(
        t,
        *_base(t),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("field", sa.String(60), nullable=False),
        sa.Column("source_quote", sa.String(500), nullable=True),
        sa.Column("intake_message_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "demand_id", "field", name="uq_demand_field_sources_field"),
        _fk("demand_id", "demands", "fk_demand_field_sources_demand_id_demands", "CASCADE"),
        _fk(
            "intake_message_id",
            "demand_intake_messages",
            "fk_demand_field_sources_intake_message",
            "SET NULL",
        ),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])

    t = "demand_suggestions"
    op.create_table(
        t,
        *_base(t),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        _fk("demand_id", "demands", "fk_demand_suggestions_demand_id_demands", "CASCADE"),
        _fk("decided_by", "users", "fk_demand_suggestions_decided_by_users"),
        sa.CheckConstraint(_in("kind", SUGGESTION_KINDS), name=op.f("ck_demand_suggestions_kind")),
        sa.CheckConstraint(_in("status", SUGGESTION_STATUSES), name=op.f("ck_demand_suggestions_status")),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index("ix_demand_suggestions_demand_id", t, ["demand_id"])
    op.create_index(
        "uq_demand_suggestions_pending", t, ["tenant_id", "demand_id", "dedupe_key"], unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )  # fmt: skip

    for table in TABLES:
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
