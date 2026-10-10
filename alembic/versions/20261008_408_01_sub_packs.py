# ruff: noqa: E501
"""Submission Agent (Epic 61, FR-305..FR-307): per-Client submission rules and prepared submission packs.

Every table: tenant RLS + internal-only org RLS. No ALTER of ``submissions``, ``applications`` or any P1-P3 table.

Revision ID: 20261008_408_01_sub_packs
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_408_01_sub_packs"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_submission_agent",)
depends_on: str | Sequence[str] | None = None

TABLES = ("client_submission_rules", "submission_packs")
RESUME_FORMATS = ("either", "original", "client_formatted")
PACK_STATUSES = ("draft", "ready", "blocked")
LIFECYCLES = ("open", "used", "discarded", "stale")
RESUME_KINDS = ("original", "client_formatted")
WRITE_UP_STATUSES = ("pending", "drafted", "failed", "edited")
RATE_UNITS = ("hour", "day", "month", "year")
PREPARED_BY = ("agent", "user", "system")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


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
    t = "client_submission_rules"
    op.create_table(
        t,
        *_base(t),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("resume_format", sa.String(20), server_default=sa.text("'either'"), nullable=False),
        sa.Column("mask_contact_details", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("write_up_max_words", sa.SmallInteger(), server_default=sa.text("150"), nullable=False),
        sa.Column("write_up_instructions", sa.String(1000), nullable=True),
        sa.Column("duplicate_window_days", sa.SmallInteger(), nullable=True),
        sa.Column("require_candidate_consent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("show_notice_period", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "client_id", name="uq_client_submission_rules_tenant_id_client_id"),
        _fk("client_id", "clients", "fk_client_submission_rules_client_id"),
        _ck(t, "resume_format", _in("resume_format", RESUME_FORMATS)),
        _ck(t, "words", "write_up_max_words BETWEEN 40 AND 400"),
        _ck(t, "dup_days", "duplicate_window_days IS NULL OR duplicate_window_days BETWEEN 1 AND 730"),
    )
    op.create_index("ix_client_submission_rules_tenant_id", t, ["tenant_id"])

    t = "submission_packs"
    op.create_table(
        t,
        *_base(t),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("lifecycle", sa.String(10), server_default=sa.text("'open'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("resume_kind", sa.String(20), nullable=True),
        sa.Column("resume_id", sa.Uuid(), nullable=True),
        sa.Column("resume_version", sa.Integer(), nullable=True),
        sa.Column(
            "document_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("write_up_status", sa.String(10), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("pay_rate", sa.Numeric(14, 2), nullable=True),
        sa.Column("bill_rate", sa.Numeric(14, 2), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("pay_rate_unit", sa.String(10), nullable=True),
        sa.Column("bill_rate_unit", sa.String(10), nullable=True),
        sa.Column("email_subject", sa.String(300), nullable=True),
        sa.Column("checks_detail", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column(
            "missing",
            postgresql.ARRAY(sa.String(40)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("blocker_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("warning_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_request_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prepared_by_kind", sa.String(10), nullable=False),
        sa.Column("prepared_by_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("opened_in_dialog_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submission_id", sa.Uuid(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_changes", postgresql.JSONB(), nullable=True),
        sa.Column("discarded_reason", sa.String(40), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_submission_packs_tenant_id_human_id"),
        _fk("application_id", "applications", "fk_submission_packs_application_id"),
        _fk("candidate_id", "candidates", "fk_submission_packs_candidate_id"),
        _fk("demand_id", "demands", "fk_submission_packs_demand_id"),
        _fk("client_id", "clients", "fk_submission_packs_client_id"),
        _fk("submission_id", "submissions", "fk_submission_packs_submission_id"),
        _ck(t, "status", _in("status", PACK_STATUSES)),
        _ck(t, "lifecycle", _in("lifecycle", LIFECYCLES)),
        _ck(t, "resume_kind", f"resume_kind IS NULL OR {_in('resume_kind', RESUME_KINDS)}"),
        _ck(t, "write_up_status", _in("write_up_status", WRITE_UP_STATUSES)),
        _ck(t, "pay_unit", f"pay_rate_unit IS NULL OR {_in('pay_rate_unit', RATE_UNITS)}"),
        _ck(t, "bill_unit", f"bill_rate_unit IS NULL OR {_in('bill_rate_unit', RATE_UNITS)}"),
        _ck(t, "prepared_by", _in("prepared_by_kind", PREPARED_BY)),
        _ck(t, "summary_len", "summary_text IS NULL OR char_length(summary_text) <= 3000"),
        _ck(t, "used", "lifecycle <> 'used' OR submission_id IS NOT NULL"),
    )
    op.create_index("ix_submission_packs_tenant_id", t, ["tenant_id"])
    op.create_index(
        "uq_submission_packs_open", t, ["tenant_id", "application_id"], unique=True,
        postgresql_where=sa.text("lifecycle = 'open'"),
    )  # fmt: skip
    op.create_index("ix_submission_packs_tenant_status", t, ["tenant_id", "status"])
    op.create_index("ix_submission_packs_client_id", t, ["client_id"])
    op.create_index("ix_submission_packs_submission_id", t, ["submission_id"])
    op.create_index("ix_submission_packs_candidate_id", t, ["candidate_id"])
    op.create_index("ix_submission_packs_application_id", t, ["application_id"])

    for table in TABLES:
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
