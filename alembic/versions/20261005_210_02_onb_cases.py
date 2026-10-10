# ruff: noqa: E501
"""Epic 23 (Digital Onboarding) 210_02: onboarding cases, tasks and policy acknowledgements.

* FKs only to the contract-layer ``placements`` and the P1 ``candidates`` (AD-16); document / signature /
  credential / P1 task ids are soft uuids.
* Worker org RLS on ``candidate_id`` (inherited from the case); a RESTRICTIVE policy hides internal
  (``assignee='user'``) tasks from Worker sessions. Acknowledgements are append-only (IP / UA erasable).

Revision ID: 20261005_210_02_onb_cases
Revises: 20261005_210_01_onb_templates
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    InheritRef,
    disable_immutability,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261005_210_02_onb_cases"
down_revision: str | tuple[str, ...] | None = "20261005_210_01_onb_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASK_KINDS = "'personal_details', 'bank_details', 'tax_details', 'emergency_contact', 'document_upload', 'esign', 'policy_ack', 'client_form', 'credential', 'internal'"
ASSIGNEE_POLICY = "onboarding_worker_assignee"


def _base(table: str) -> list[object]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
    ]


def upgrade() -> None:
    op.create_table(
        "onboarding_cases",
        *_base("onboarding_cases"),  # type: ignore[arg-type]
        sa.Column("human_id", sa.String(20), nullable=True),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("template_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'not_started'"), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress_pct", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_reason", sa.String(200), nullable=True),
        sa.Column("pending_requirements", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_onboarding_cases_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "placement_id", name="uq_onboarding_cases_placement_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_onboarding_cases_human_id"),
        sa.CheckConstraint("status IN ('not_started', 'in_progress', 'complete', 'cancelled')", name="ck_onboarding_cases_status"),
        sa.CheckConstraint("progress_pct BETWEEN 0 AND 100", name="ck_onboarding_cases_progress_pct"),
        sa.ForeignKeyConstraint(["tenant_id", "placement_id"], ["placements.tenant_id", "placements.id"], name="fk_onboarding_cases_placement", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "candidate_id"], ["candidates.tenant_id", "candidates.id"], name="fk_onboarding_cases_candidate", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "template_id"], ["onboarding_templates.tenant_id", "onboarding_templates.id"], name="fk_onboarding_cases_template", ondelete="RESTRICT"),
    )
    op.create_index("ix_onboarding_cases_tenant_id", "onboarding_cases", ["tenant_id"])
    op.create_index("ix_onboarding_cases_candidate_id", "onboarding_cases", ["candidate_id"])

    op.create_table(
        "onboarding_tasks",
        *_base("onboarding_tasks"),  # type: ignore[arg-type]
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(160), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("mandatory", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("assignee", sa.String(10), server_default=sa.text("'worker'"), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("p1_task_id", sa.Uuid(), nullable=True),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("source", sa.String(12), server_default=sa.text("'template'"), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_approve", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reusable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("validity_days", sa.Integer(), nullable=True),
        sa.Column("config", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("draft", postgresql.JSONB(), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("document_ids", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'::uuid[]"), nullable=False),
        sa.Column("signature_request_id", sa.Uuid(), nullable=True),
        sa.Column("credential_id", sa.Uuid(), nullable=True),
        sa.Column("return_comment", sa.Text(), nullable=True),
        sa.Column("waived_reason", sa.Text(), nullable=True),
        sa.Column("decided_by_kind", sa.String(10), nullable=True),
        sa.Column("decided_by_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reused_from_task_id", sa.Uuid(), nullable=True),
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_onboarding_tasks_tenant_id_id"),
        sa.UniqueConstraint("case_id", "key", name="uq_onboarding_tasks_case_id_key"),
        sa.CheckConstraint(f"kind IN ({TASK_KINDS})", name="ck_onboarding_tasks_kind"),
        sa.CheckConstraint("assignee IN ('worker', 'user')", name="ck_onboarding_tasks_assignee"),
        sa.CheckConstraint("status IN ('pending', 'submitted', 'returned', 'approved', 'waived')", name="ck_onboarding_tasks_status"),
        sa.CheckConstraint("source IN ('template', 'compliance', 'user')", name="ck_onboarding_tasks_source"),
        sa.CheckConstraint("decided_by_kind IS NULL OR decided_by_kind IN ('user', 'system')", name="ck_onboarding_tasks_decided_by_kind"),
        sa.ForeignKeyConstraint(["tenant_id", "case_id"], ["onboarding_cases.tenant_id", "onboarding_cases.id"], name="fk_onboarding_tasks_case", ondelete="CASCADE"),
    )
    op.create_index("ix_onboarding_tasks_tenant_id", "onboarding_tasks", ["tenant_id"])
    op.create_index("ix_onboarding_tasks_case_id", "onboarding_tasks", ["case_id"])
    op.create_index("ix_onboarding_tasks_candidate_id", "onboarding_tasks", ["candidate_id"])
    op.create_index("ix_onboarding_tasks_p1_task_id", "onboarding_tasks", ["p1_task_id"])
    op.create_index("ix_onboarding_tasks_signature_request_id", "onboarding_tasks", ["signature_request_id"])

    op.create_table(
        "onboarding_acknowledgements",
        *_base("onboarding_acknowledgements"),  # type: ignore[arg-type]
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("document_template_id", sa.Uuid(), nullable=False),
        sa.Column("template_version_id", sa.Uuid(), nullable=False),
        sa.Column("rendered_sha256", sa.String(64), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id", "task_id"], ["onboarding_tasks.tenant_id", "onboarding_tasks.id"], name="fk_onboarding_acknowledgements_task", ondelete="RESTRICT"),
    )
    op.create_index("ix_onboarding_acknowledgements_tenant_id", "onboarding_acknowledgements", ["tenant_id"])
    op.create_index("ix_onboarding_acknowledgements_task_id", "onboarding_acknowledgements", ["task_id"])
    op.create_index("ix_onboarding_acknowledgements_candidate_id", "onboarding_acknowledgements", ["candidate_id"])

    for table in ("onboarding_cases", "onboarding_tasks", "onboarding_acknowledgements"):
        enable_rls(table)
    enable_org_rls("onboarding_cases", worker_col="candidate_id")
    enable_org_rls(
        "onboarding_tasks",
        worker_col="candidate_id",
        inherit_from=InheritRef("onboarding_cases", "case_id", ("candidate_id",)),
    )
    enable_org_rls(
        "onboarding_acknowledgements",
        worker_col="candidate_id",
        inherit_from=InheritRef("onboarding_tasks", "task_id", ("candidate_id",)),
    )
    op.execute(
        f"CREATE POLICY {ASSIGNEE_POLICY} ON onboarding_tasks AS RESTRICTIVE FOR ALL "
        "USING (COALESCE(current_setting('app.org_kind', true), '') <> 'worker' OR assignee = 'worker') "
        "WITH CHECK (COALESCE(current_setting('app.org_kind', true), '') <> 'worker' OR assignee = 'worker')"
    )
    enable_immutability(
        "onboarding_acknowledgements", status_column=None, mutable_states=(), allowed_columns=("ip", "user_agent")
    )


def downgrade() -> None:
    disable_immutability("onboarding_acknowledgements")
    op.execute(f"DROP POLICY IF EXISTS {ASSIGNEE_POLICY} ON onboarding_tasks")
    for table in ("onboarding_acknowledgements", "onboarding_tasks", "onboarding_cases"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
