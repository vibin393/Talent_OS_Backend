# ruff: noqa: E501
"""Epic 57 (Screening Agent, story 57.1 / A8): versioned questionnaires, bindings and the tenant settings row.

* ``screening_questionnaire_versions`` -- Agent Questions per P1 questionnaire (no ALTER of ``screening_questionnaires``);
  one ``approved`` version per questionnaire (partial unique ``uq_sqv_approved``).
* ``screening_questionnaire_bindings`` -- tenant / client / demand binding (unique per scope via COALESCE).
* ``screening_agent_settings`` -- one row per tenant (expiry, reminders, threshold, stop words, channels).

Internal-only tables: tenant RLS + internal org RLS. Branch ``p4_screening_agent`` forks from the foundation head.

Revision ID: 20261008_404_01_screen_qnrs
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_404_01_screen_qnrs"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_screening_agent",)
depends_on: str | Sequence[str] | None = None

TABLES = ("screening_questionnaire_versions", "screening_questionnaire_bindings", "screening_agent_settings")


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


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    t = "screening_questionnaire_versions"
    op.create_table(
        t,
        *_base(t),
        sa.Column("questionnaire_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("questions", postgresql.JSONB(), nullable=False),
        sa.Column(
            "clarifications", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("status", sa.String(10), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("change_note", sa.String(500), nullable=True),
        sa.UniqueConstraint("tenant_id", "questionnaire_id", "version", name="uq_sqv_questionnaire_version"),
        _fk("questionnaire_id", "screening_questionnaires", "fk_sqv_questionnaire_id"),
        _fk("approved_by", "users", "fk_sqv_approved_by_users"),
        _ck(t, "status", "status IN ('draft', 'approved', 'retired')"),
        _ck(t, "approved_by", "status <> 'approved' OR approved_by IS NOT NULL"),
        _ck(t, "version", "version >= 1"),
    )
    op.create_index(
        "uq_sqv_approved",
        t,
        ["tenant_id", "questionnaire_id"],
        unique=True,
        postgresql_where=sa.text("status = 'approved'"),
    )

    t = "screening_questionnaire_bindings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("questionnaire_id", sa.Uuid(), nullable=False),
        sa.Column("scope_kind", sa.String(10), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=True),
        _fk("questionnaire_id", "screening_questionnaires", "fk_sqb_questionnaire_id"),
        _ck(t, "scope_kind", "scope_kind IN ('tenant', 'client', 'demand')"),
        _ck(t, "scope_id", "(scope_kind = 'tenant') = (scope_id IS NULL)"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sqb_scope ON screening_questionnaire_bindings "
        "(tenant_id, scope_kind, COALESCE(scope_id, '00000000-0000-0000-0000-000000000000'::uuid))"
    )

    t = "screening_agent_settings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("expiry_days", sa.SmallInteger(), server_default=sa.text("7"), nullable=False),
        sa.Column("reminder_after_hours", sa.SmallInteger(), server_default=sa.text("24"), nullable=False),
        sa.Column("max_reminders", sa.SmallInteger(), server_default=sa.text("2"), nullable=False),
        sa.Column("questions_per_turn", sa.SmallInteger(), server_default=sa.text("2"), nullable=False),
        sa.Column(
            "low_confidence_threshold", sa.Numeric(3, 2), server_default=sa.text("0.70"), nullable=False
        ),
        sa.Column("stop_words", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("whatsapp_invite_template_id", sa.Uuid(), nullable=True),
        sa.Column("default_channel", sa.String(12), server_default=sa.text("'auto'"), nullable=False),
        sa.Column("allow_campus_candidates", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", name="uq_screening_agent_settings_tenant_id"),
        _ck(t, "expiry_days", "expiry_days BETWEEN 1 AND 14"),
        _ck(t, "reminder_after_hours", "reminder_after_hours BETWEEN 4 AND 72"),
        _ck(t, "max_reminders", "max_reminders BETWEEN 0 AND 2"),
        _ck(t, "questions_per_turn", "questions_per_turn BETWEEN 1 AND 3"),
        _ck(t, "low_confidence_threshold", "low_confidence_threshold BETWEEN 0.5 AND 0.95"),
        _ck(
            t,
            "default_channel",
            "default_channel IN ('auto', 'portal_chat', 'link_chat', 'email', 'whatsapp')",
        ),
    )

    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
