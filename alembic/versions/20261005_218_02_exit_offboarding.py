# ruff: noqa: E501
"""Epic 31 218_02: ``offboarding_templates``, ``offboarding_cases``, ``offboarding_items``, ``offboarding_feedback``
and ``conversions`` (FR-164..FR-166).

One open case per placement (partial UNIQUE). Cases inherit ``client_id`` / ``candidate_id`` from the placement and
items from their case (``tos_inherit_org``) for Client Portal / Worker org RLS. The default template is seeded per
tenant on first use (``offboarding.default_template``), so provisioning needs no hook.

Revision ID: 20261005_218_02_exit_offboarding
Revises: 20261005_218_01_exit_extensions
Create Date: 2026-10-05
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_218_02_exit_offboarding"
down_revision: str | tuple[str, ...] | None = "20261005_218_01_exit_extensions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "offboarding_templates",
        *_base("offboarding_templates"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("items", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_offboarding_templates_tenant_id_id"),
    )
    op.create_index("ix_offboarding_templates_tenant_id", "offboarding_templates", ["tenant_id"])
    op.create_index(
        "uq_offboarding_templates_default",
        "offboarding_templates",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    enable_rls("offboarding_templates")

    op.create_table(
        "offboarding_cases",
        *_base("offboarding_cases"),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(30), nullable=False),
        sa.Column("reason_note", sa.Text(), nullable=True),
        sa.Column("notice_date", sa.Date(), nullable=True),
        sa.Column("requested_lwd", sa.Date(), nullable=True),
        sa.Column("last_working_day", sa.Date(), nullable=False),
        sa.Column("previous_end_date", sa.Date(), nullable=True),
        sa.Column("notice_period_days_snapshot", sa.Integer(), nullable=True),
        sa.Column("notice_shortfall_days", sa.Integer(), nullable=True),
        sa.Column("early_termination_by", sa.String(10), nullable=True),
        sa.Column("early_attrition", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("bench_eligible", sa.Boolean(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'initiated'"), nullable=False),
        sa.Column("initiated_by_kind", sa.String(20), nullable=False),
        sa.Column("initiated_by", sa.Uuid(), nullable=True),
        sa.Column("client_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.Uuid(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column(
            "access_systems", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "placement_id"],
            ["placements.tenant_id", "placements.id"],
            name="fk_offboarding_cases_placement",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_offboarding_cases_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_offboarding_cases_tenant_id_human_id"),
        sa.CheckConstraint(
            "reason IN ('end_of_term', 'client_termination', 'worker_resignation', 'converted', 'misconduct', 'other')",
            name="ck_offboarding_cases_reason",
        ),
        sa.CheckConstraint(
            "status IN ('initiated', 'in_progress', 'ready_to_close', 'closed', 'cancelled')",
            name="ck_offboarding_cases_status",
        ),
        sa.CheckConstraint(
            "early_termination_by IS NULL OR early_termination_by IN ('client', 'worker')",
            name="ck_offboarding_cases_et_by",
        ),
    )
    op.create_index("ix_offboarding_cases_tenant_id", "offboarding_cases", ["tenant_id"])
    op.create_index("ix_offboarding_cases_placement_id", "offboarding_cases", ["placement_id"])
    op.create_index(
        "uq_offboarding_cases_open",
        "offboarding_cases",
        ["tenant_id", "placement_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('initiated', 'in_progress', 'ready_to_close')"),
    )
    enable_rls("offboarding_cases")
    enable_org_rls(
        "offboarding_cases",
        client_col="client_id",
        worker_col="candidate_id",
        inherit_from=InheritRef("placements", "placement_id", ("client_id", "candidate_id")),
    )

    op.create_table(
        "offboarding_items",
        *_base("offboarding_items"),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("mandatory", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("owner_role", sa.String(30), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("verifier", sa.String(60), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("waived_reason", sa.Text(), nullable=True),
        sa.Column("completed_by_kind", sa.String(20), nullable=True),
        sa.Column("completed_by_id", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_file_id", sa.Uuid(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("auto_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("verifier_details", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["offboarding_cases.tenant_id", "offboarding_cases.id"],
            name="fk_offboarding_items_case",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_file_id"],
            ["stored_files.id"],
            name="fk_offboarding_items_evidence_file_id_stored_files",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("case_id", "kind", name="uq_offboarding_items_case_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'waived', 'not_applicable')", name="ck_offboarding_items_status"
        ),
        sa.CheckConstraint(
            "completed_by_kind IS NULL OR completed_by_kind IN ('user', 'system', 'client_user', 'secure_link')",
            name="ck_offboarding_items_completed_by",
        ),
        sa.CheckConstraint(
            "status <> 'waived' OR waived_reason IS NOT NULL", name="ck_offboarding_items_waive_reason"
        ),
    )
    op.create_index("ix_offboarding_items_tenant_id", "offboarding_items", ["tenant_id"])
    op.create_index("ix_offboarding_items_case_id", "offboarding_items", ["case_id"])
    enable_rls("offboarding_items")
    enable_org_rls(
        "offboarding_items",
        client_col="client_id",
        worker_col="candidate_id",
        inherit_from=InheritRef("offboarding_cases", "case_id", ("client_id", "candidate_id")),
    )

    op.create_table(
        "offboarding_feedback",
        *_base("offboarding_feedback"),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("answers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("recorded_by_kind", sa.String(20), nullable=False),
        sa.Column("recorded_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["offboarding_cases.tenant_id", "offboarding_cases.id"],
            name="fk_offboarding_feedback_case",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("case_id", name="uq_offboarding_feedback_case"),
    )
    op.create_index("ix_offboarding_feedback_tenant_id", "offboarding_feedback", ["tenant_id"])
    enable_rls("offboarding_feedback")

    op.create_table(
        "conversions",
        *_base("conversions"),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("conversion_date", sa.Date(), nullable=False),
        sa.Column("offered_compensation", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column(
            "fee_rule_snapshot", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("fee_proposal", sa.Numeric(16, 2), nullable=True),
        sa.Column("offboarding_case_id", sa.Uuid(), nullable=True),
        sa.Column("recorded_by", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "placement_id"],
            ["placements.tenant_id", "placements.id"],
            name="fk_conversions_placement",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "offboarding_case_id"],
            ["offboarding_cases.tenant_id", "offboarding_cases.id"],
            name="fk_conversions_case",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "placement_id", name="uq_conversions_placement"),
        sa.CheckConstraint("offered_compensation >= 0", name="ck_conversions_comp"),
    )
    op.create_index("ix_conversions_tenant_id", "conversions", ["tenant_id"])
    enable_rls("conversions")


def downgrade() -> None:
    disable_rls("conversions")
    op.drop_table("conversions")
    disable_rls("offboarding_feedback")
    op.drop_table("offboarding_feedback")
    disable_org_rls("offboarding_items")
    disable_rls("offboarding_items")
    op.drop_table("offboarding_items")
    disable_org_rls("offboarding_cases")
    disable_rls("offboarding_cases")
    op.drop_table("offboarding_cases")
    disable_rls("offboarding_templates")
    op.drop_table("offboarding_templates")
