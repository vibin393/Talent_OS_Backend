# ruff: noqa: E501
"""Epic 23 (Digital Onboarding) 210_01: onboarding settings, templates and template tasks (internal only).

Revision ID: 20261005_210_01_onb_templates
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_210_01_onb_templates"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_onboarding",)
depends_on: str | Sequence[str] | None = None

TASK_KINDS = "'personal_details', 'bank_details', 'tax_details', 'emergency_contact', 'document_upload', 'esign', 'policy_ack', 'client_form', 'credential', 'internal'"
OWNER_KINDS = "'role', 'placement_account_manager', 'placement_recruiter', 'case_owner'"


def _base(table: str) -> list[object]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
    ]


def upgrade() -> None:
    op.create_table(
        "onboarding_settings",
        *_base("onboarding_settings"),  # type: ignore[arg-type]
        sa.Column("auto_invite", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("worker_reminder_every_days", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column("default_owner_by_role", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("default_hr_owner_id", sa.Uuid(), nullable=True),
        sa.Column("default_tenant_signatory_user_id", sa.Uuid(), nullable=True),
        sa.Column("reuse_validity_days", sa.Integer(), server_default=sa.text("365"), nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_onboarding_settings_tenant_id"),
    )
    op.create_index("ix_onboarding_settings_tenant_id", "onboarding_settings", ["tenant_id"])

    op.create_table(
        "onboarding_templates",
        *_base("onboarding_templates"),  # type: ignore[arg-type]
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("worker_type", sa.String(20), nullable=True),
        sa.Column("country", sa.String(2), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_onboarding_templates_tenant_id_id"),
    )
    op.create_index("ix_onboarding_templates_tenant_id", "onboarding_templates", ["tenant_id"])

    op.create_table(
        "onboarding_template_tasks",
        *_base("onboarding_template_tasks"),  # type: ignore[arg-type]
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("mandatory", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("assignee", sa.String(10), server_default=sa.text("'worker'"), nullable=False),
        sa.Column("owner_kind", sa.String(30), nullable=True),
        sa.Column("owner_role_key", sa.String(60), nullable=True),
        sa.Column("due_offset_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("auto_approve", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reusable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("validity_days", sa.Integer(), nullable=True),
        sa.Column("config", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.UniqueConstraint("template_id", "key", name="uq_onboarding_template_tasks_template_id_key"),
        sa.CheckConstraint(f"kind IN ({TASK_KINDS})", name="ck_onboarding_template_tasks_kind"),
        sa.CheckConstraint("assignee IN ('worker', 'user')", name="ck_onboarding_template_tasks_assignee"),
        sa.CheckConstraint(f"owner_kind IS NULL OR owner_kind IN ({OWNER_KINDS})", name="ck_onboarding_template_tasks_owner_kind"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["onboarding_templates.tenant_id", "onboarding_templates.id"],
            name="fk_onboarding_template_tasks_template",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_onboarding_template_tasks_tenant_id", "onboarding_template_tasks", ["tenant_id"])
    op.create_index("ix_onboarding_template_tasks_template_id", "onboarding_template_tasks", ["template_id"])

    for table in ("onboarding_settings", "onboarding_templates", "onboarding_template_tasks"):
        enable_rls(table)
        enable_org_rls(table)  # internal only


def downgrade() -> None:
    for table in ("onboarding_template_tasks", "onboarding_templates", "onboarding_settings"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
