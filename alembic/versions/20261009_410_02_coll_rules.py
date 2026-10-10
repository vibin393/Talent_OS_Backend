# ruff: noqa: E501
"""Epic 63 (stories 63.4 / 63.6, FR-314 / FR-315): Client collection rules, their steps and Collections Agent holds.

* ``collection_rules`` (audited) -- per Client (``client_id`` NULL = the tenant default; one each).
* ``collection_rule_steps`` (audited) -- schedule steps; CHECK ``auto``: only a templated pre-due courteous / friendly
  step to billing contacts may be sent automatically.
* ``collection_agent_holds`` (audited) -- pause / takeover per Client or document; one active hold per pair.

Internal only: tenant RLS + internal-only org RLS. No ALTER of P2 tables (``client_collection_settings.reminder_rule_id``
stays unused; rules attach by ``collection_rules.client_id``).

Revision ID: 20261009_410_02_coll_rules
Revises: 20261009_410_01_ts_flags
Create Date: 2026-10-09
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261009_410_02_coll_rules"
down_revision: str | tuple[str, ...] | None = "20261009_410_01_ts_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("collection_rules", "collection_rule_steps", "collection_agent_holds")
AUTO_CHECK = (
    "allow_auto = false OR (offset_days < 0 AND email_template_id IS NOT NULL "
    "AND audience = 'billing_contacts' AND tone IN ('courteous', 'friendly'))"
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


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    t = "collection_rules"
    op.create_table(
        t,
        *_base(t),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("escalation_contact_id", sa.Uuid(), nullable=True),
        sa.Column("max_reminders_per_week", sa.SmallInteger(), server_default=sa.text("2"), nullable=False),
        sa.Column("quiet_start", sa.Time(), server_default=sa.text("'18:00'"), nullable=False),
        sa.Column("quiet_end", sa.Time(), server_default=sa.text("'09:00'"), nullable=False),
        sa.Column("timezone", sa.String(64), server_default=sa.text("'Asia/Kolkata'"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "client_id", name="uq_collection_rules_client"),
        _fk("client_id", "clients", "fk_collection_rules_client_id", "CASCADE"),
        _fk("escalation_contact_id", "contacts", "fk_collection_rules_escalation_contact_id"),
        _ck(t, "weekly", "max_reminders_per_week BETWEEN 1 AND 7"),
    )
    op.create_index(
        "uq_collection_rules_default", t, ["tenant_id"], unique=True, postgresql_where=sa.text("client_id IS NULL")
    )

    t = "collection_rule_steps"
    op.create_table(
        t,
        *_base(t),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("offset_days", sa.SmallInteger(), nullable=False),
        sa.Column("tone", sa.String(12), nullable=False),
        sa.Column("audience", sa.String(20), nullable=False),
        sa.Column("email_template_id", sa.Uuid(), nullable=True),
        sa.Column("allow_auto", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("create_task", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.UniqueConstraint("tenant_id", "rule_id", "position", name="uq_collection_rule_steps_position"),
        _fk("rule_id", "collection_rules", "fk_collection_rule_steps_rule_id", "CASCADE"),
        sa.ForeignKeyConstraint(
            ["email_template_id"], ["email_templates.id"], name="fk_collection_rule_steps_email_template_id_email_templates",
            ondelete="SET NULL",
        ),
        _ck(t, "offset", "offset_days BETWEEN -30 AND 180"),
        _ck(t, "tone", "tone IN ('courteous', 'friendly', 'firm', 'escalation')"),
        _ck(t, "audience", "audience IN ('billing_contacts', 'escalation_contact', 'both')"),
        _ck(t, "auto", AUTO_CHECK),
    )  # fmt: skip
    op.create_index("ix_collection_rule_steps_rule_id", t, ["rule_id"])

    t = "collection_agent_holds"
    op.create_table(
        t,
        *_base(t),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("document_kind", sa.String(12), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.Uuid(), nullable=True),
        _fk("client_id", "clients", "fk_collection_agent_holds_client_id", "CASCADE"),
        _ck(t, "mode", "mode IN ('paused', 'takeover')"),
        _ck(t, "doc_kind", "document_kind IS NULL OR document_kind IN ('invoice', 'debit_note')"),
        _ck(t, "doc_ref", "(document_id IS NULL) = (document_kind IS NULL)"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_collection_agent_holds_active ON collection_agent_holds "
        "(tenant_id, client_id, coalesce(document_id, '00000000-0000-0000-0000-000000000000'::uuid)) "
        "WHERE released_at IS NULL"
    )
    op.create_index("ix_collection_agent_holds_client_id", t, ["client_id"])

    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
