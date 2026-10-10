"""Epic 62 (stories 62.1-62.3, FR-308 / FR-309): compliance chases and BGV provider follow-ups.

* ``compliance_chases`` (audited) -- one templated reminder per item and cadence offset.
* ``bgv_follow_ups`` (audited) -- status requests to BGV providers about overdue checks.

Internal tables only: tenant RLS + internal-only org RLS. No ALTER of any P1-P3 table.

Revision ID: 20261008_409_03_chases
Revises: 20261008_409_02_onb_qa
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_409_03_chases"
down_revision: str | tuple[str, ...] | None = "20261008_409_02_onb_qa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("compliance_chases", "bgv_follow_ups")


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


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Uuid(), nullable=nullable)


def upgrade() -> None:
    t = "compliance_chases"
    op.create_table(
        t,
        *_base(t),
        _uuid("candidate_id", False),
        _uuid("placement_id"),
        sa.Column("item_kind", sa.String(20), nullable=False),
        _uuid("item_ref", False),
        sa.Column("item_label", sa.String(200), nullable=False),
        sa.Column("due_basis", sa.String(12), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("offset_days", sa.SmallInteger(), nullable=False),
        sa.Column(
            "channels",
            postgresql.ARRAY(sa.String(10)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("suppressed", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("escalated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _uuid("task_id"),
        _uuid("agent_run_id"),
        sa.Column("message_refs", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "item_kind", "item_ref", "offset_days", name="uq_compliance_chases_item_offset"
        ),
        _fk("candidate_id", "candidates", "fk_compliance_chases_candidate_id"),
        _ck(t, "item_kind", "item_kind IN ('checklist_item', 'credential', 'permit')"),
        _ck(t, "due_basis", "due_basis IN ('start_date', 'expiry')"),
    )
    op.create_index("ix_compliance_chases_candidate_sent", t, ["candidate_id", "sent_at"])
    op.create_index("ix_compliance_chases_placement_id", t, ["placement_id"])

    t = "bgv_follow_ups"
    op.create_table(
        t,
        *_base(t),
        _uuid("check_id", False),
        _uuid("case_id", False),
        _uuid("candidate_id", False),
        _uuid("provider_id"),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("seq", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'sent'"), nullable=False),
        sa.Column("provider_status", sa.String(20), nullable=True),
        _uuid("email_message_id"),
        _uuid("agent_run_id"),
        _uuid("proposal_id"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "check_id", "seq", name="uq_bgv_follow_ups_check_seq"),
        _fk("check_id", "bgv_checks", "fk_bgv_follow_ups_check_id", "CASCADE"),
        _fk("case_id", "bgv_cases", "fk_bgv_follow_ups_case_id", "CASCADE"),
        _ck(t, "channel", "channel IN ('connector', 'email')"),
        _ck(t, "status", "status IN ('sent', 'failed', 'status_received')"),
    )
    op.create_index("ix_bgv_follow_ups_case_id", t, ["case_id"])

    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
