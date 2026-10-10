# ruff: noqa: E501
"""Epic 15 (Sales CRM) 202_02: leads + status history (story 2.1); P1 ``tasks.entity_type`` gains
``lead`` / ``opportunity`` (Tasks attach to CRM records through the activity entity registry).

The tasks CHECK is widened *additively*: the current definition is read and the two values appended, so a
parallel Phase 2 branch that widens the same CHECK is not undone.

Revision ID: 20261004_202_02_crm_leads
Revises: 20261004_202_01_crm_settings
Create Date: 2026-10-04
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261004_202_02_crm_leads"
down_revision: str | tuple[str, ...] | None = "20261004_202_01_crm_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CRM_TASK_TYPES = ("lead", "opportunity")


def _task_entity_types() -> list[str]:
    row = op.get_bind().execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = 'tasks' AND c.conname = 'ck_tasks_entity_type'"
        )
    ).scalar_one_or_none()
    return re.findall(r"'([a-z_]+)'", row or "")


def _set_task_types(values: list[str]) -> None:
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_tasks_entity_type")
    listed = ", ".join(f"'{v}'" for v in values)
    op.execute(f"ALTER TABLE tasks ADD CONSTRAINT ck_tasks_entity_type CHECK (entity_type IS NULL OR entity_type IN ({listed}))")


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("company_name", postgresql.CITEXT(), nullable=False),
        sa.Column("website", sa.String(500), nullable=True),
        sa.Column("website_domain", sa.String(255), nullable=True),
        sa.Column("gstin", sa.String(15), nullable=True),
        sa.Column("industry", sa.String(200), nullable=True),
        sa.Column("location", sa.String(300), nullable=True),
        sa.Column("contact_name", sa.String(200), nullable=True),
        sa.Column("contact_title", sa.String(200), nullable=True),
        sa.Column("contact_email", postgresql.CITEXT(), nullable=True),
        sa.Column("contact_phone", sa.String(20), nullable=True),
        sa.Column("source_code", sa.String(40), nullable=True),
        sa.Column("estimated_roles", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("estimated_positions", sa.Integer(), nullable=True),
        sa.Column("expected_annual_revenue", sa.Numeric(16, 2), nullable=True),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'INR'"), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'new'"), nullable=False),
        sa.Column("disqualify_reason_code", sa.String(40), nullable=True),
        sa.Column("disqualify_note", sa.Text(), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_opportunity_id", sa.Uuid(), nullable=True),
        sa.Column("converted_client_id", sa.Uuid(), nullable=True),
        sa.Column("converted_contact_id", sa.Uuid(), nullable=True),
        sa.Column("duplicate_ack_by", sa.Uuid(), nullable=True),
        sa.Column("import_batch_id", sa.Uuid(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_leads"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_leads_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "owner_id"], ["users.tenant_id", "users.id"], name="fk_leads_owner_id_users", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "converted_client_id"], ["clients.tenant_id", "clients.id"], name="fk_leads_converted_client_id_clients", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "converted_contact_id"], ["contacts.tenant_id", "contacts.id"], name="fk_leads_converted_contact_id_contacts", ondelete="RESTRICT"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_leads_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_leads_tenant_id_human_id"),
        sa.CheckConstraint("status IN ('new', 'contacted', 'qualified', 'converted', 'disqualified')", name="ck_leads_status"),
        sa.CheckConstraint("estimated_positions IS NULL OR estimated_positions >= 0", name="ck_leads_positions"),
    )
    op.create_index("ix_leads_tenant_id", "leads", ["tenant_id"])
    op.execute("CREATE INDEX ix_leads_company_name_trgm ON leads USING gin ((company_name::text) gin_trgm_ops)")
    op.create_index("ix_leads_tenant_domain", "leads", ["tenant_id", "website_domain"])
    op.create_index("ix_leads_tenant_gstin", "leads", ["tenant_id", "gstin"])
    op.create_index("ix_leads_tenant_status_owner", "leads", ["tenant_id", "status", "owner_id"])
    op.create_index("ix_leads_tenant_source_created", "leads", ["tenant_id", "source_code", "created_at"])

    op.create_table(
        "crm_status_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("entity_kind", sa.String(12), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("from_value", sa.String(16), nullable=True),
        sa.Column("to_value", sa.String(16), nullable=False),
        sa.Column("actor_kind", sa.String(10), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("reason_code", sa.String(40), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("competitor", sa.String(200), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_crm_status_history"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_crm_status_history_tenant_id_tenants", ondelete="RESTRICT"),
        sa.CheckConstraint("entity_kind IN ('lead', 'opportunity')", name="ck_crm_status_history_entity_kind"),
    )
    op.create_index("ix_crm_status_history_tenant_id", "crm_status_history", ["tenant_id"])
    op.create_index("ix_crm_status_history_entity", "crm_status_history", ["tenant_id", "entity_kind", "entity_id", "at"])

    for table in ("leads", "crm_status_history"):
        enable_rls(table)
        enable_org_rls(table)  # internal-only

    current = _task_entity_types()
    _set_task_types(current + [v for v in CRM_TASK_TYPES if v not in current])


def downgrade() -> None:
    op.execute("DELETE FROM tasks WHERE entity_type IN ('lead', 'opportunity')")
    _set_task_types([v for v in _task_entity_types() if v not in CRM_TASK_TYPES])
    for table in ("crm_status_history", "leads"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
