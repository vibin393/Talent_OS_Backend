"""Epic 64 (FR-316..FR-318): ``analytics_answers`` -- private answers of the Analytics agent (definition, viz,
explanation and run metadata; never result rows).

First revision of branch ``p4_analytics_agent`` (band 411) on the Phase 4 foundation head. Tenant RLS plus the
internal-only org policy. ``conversation_id`` / ``run_id`` are plain UUIDs (other epics' tables, no FK);
``saved_report_id`` -> ``report_definitions`` ``ON DELETE SET NULL``.

Revision ID: 20261008_411_01_analytics_ans
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_411_01_analytics_ans"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_analytics_agent",)
depends_on: str | Sequence[str] | None = None

T = "analytics_answers"
_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        T,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("question", sa.String(500), nullable=False),
        sa.Column("assumptions", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("source_key", sa.String(80), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("definition_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("viz", postgresql.JSONB(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("masked_fields", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("limit_kind", sa.String(10), nullable=True),
        sa.Column("suppressed_cells", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'ok'"), nullable=False),
        sa.Column("saved_report_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{T}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{T}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{T}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_analytics_answers_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["saved_report_id"],
            ["report_definitions.id"],
            name="fk_analytics_answers_saved_report_id_report_definitions",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "limit_kind IS NULL OR limit_kind IN ('row_cap', 'timeout')",
            name=op.f("ck_analytics_answers_limit_kind"),
        ),
        sa.CheckConstraint("status IN ('ok', 'needs_repair')", name=op.f("ck_analytics_answers_status")),
    )
    op.create_index(f"ix_{T}_tenant_id", T, ["tenant_id"])
    op.create_index(
        "ix_analytics_answers_user_created", T, ["tenant_id", "user_id", sa.text("created_at DESC")]
    )
    op.create_index("ix_analytics_answers_conversation_id", T, ["conversation_id"])
    op.create_index("ix_analytics_answers_expires_at", T, ["expires_at"])
    enable_rls(T)
    enable_org_rls(T)


def downgrade() -> None:
    disable_org_rls(T)
    disable_rls(T)
    op.drop_table(T)
