# ruff: noqa: E501
"""Epic 30 217_03: ``collection_activities``, ``client_collection_settings``, ``accounting_export_templates``,
``accounting_exports``, ``accounting_export_items``, ``reconciliation_runs``, ``reconciliation_findings``,
``period_close_events`` (FR-156, FR-160, FR-161, NFR-25). All operational (mutable, audited); tenant RLS.

Revision ID: 20261005_217_03_rcv_coll_close
Revises: 20261005_217_02_rcv_tds_wo
Create Date: 2026-10-05
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261005_217_03_rcv_coll_close"
down_revision: str | tuple[str, ...] | None = "20261005_217_02_rcv_tds_wo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "collection_activities",
    "client_collection_settings",
    "accounting_export_templates",
    "accounting_exports",
    "accounting_export_items",
    "reconciliation_runs",
    "reconciliation_findings",
    "period_close_events",
)


def base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
    ]


def tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


def jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(default), nullable=False)


def upgrade() -> None:
    op.create_table(
        "collection_activities",
        *base("collection_activities"),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("document_kind", sa.String(12), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("ptp_amount", sa.Numeric(16, 2), nullable=True),
        sa.Column("ptp_currency", sa.CHAR(3), nullable=True),
        sa.Column("ptp_date", sa.Date(), nullable=True),
        sa.Column("ptp_status", sa.String(8), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint("kind IN ('call', 'email', 'meeting', 'note', 'promise_to_pay')", name="ck_collection_activities_kind"),
        sa.CheckConstraint("ptp_status IS NULL OR ptp_status IN ('open', 'kept', 'broken')", name="ck_collection_activities_ptp_status"),
        sa.CheckConstraint("kind <> 'promise_to_pay' OR (ptp_amount IS NOT NULL AND ptp_date IS NOT NULL)", name="ck_collection_activities_ptp_fields"),
        sa.CheckConstraint("document_kind IS NULL OR document_kind IN ('invoice', 'debit_note')", name="ck_collection_activities_document_kind"),
        tfk("client_id", "clients", "fk_collection_activities_client_id_clients"),
    )
    op.create_index("ix_collection_activities_tenant_id", "collection_activities", ["tenant_id"])
    op.create_index("ix_collection_activities_client", "collection_activities", ["tenant_id", "client_id", "occurred_at"])
    op.create_index("ix_collection_activities_ptp", "collection_activities", ["tenant_id", "ptp_status", "ptp_date"])

    op.create_table(
        "client_collection_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("do_not_remind", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("reminder_rule_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "client_id", name="pk_client_collection_settings"),
        tfk("client_id", "clients", "fk_client_collection_settings_client_id_clients", "CASCADE"),
    )

    op.create_table(
        "accounting_export_templates",
        *base("accounting_export_templates"),
        sa.Column("name", sa.String(120), nullable=False),
        jsonb("columns", "'[]'::jsonb"),
        jsonb("account_map", "'{}'::jsonb"),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_accounting_export_templates_tenant_id_id"),
    )
    op.create_index("ix_accounting_export_templates_tenant_id", "accounting_export_templates", ["tenant_id"])

    op.create_table(
        "accounting_exports",
        *base("accounting_exports"),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("format", sa.String(4), server_default=sa.text("'csv'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("row_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("flagged_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        jsonb("rows", "'[]'::jsonb"),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_accounting_exports_tenant_id_id"),
        sa.CheckConstraint("status IN ('queued', 'completed', 'failed')", name="ck_accounting_exports_status"),
        sa.CheckConstraint("format IN ('csv', 'xlsx')", name="ck_accounting_exports_format"),
        tfk("legal_entity_id", "legal_entities", "fk_accounting_exports_le_legal_entities"),
        tfk("template_id", "accounting_export_templates", "fk_accounting_exports_template_id"),
    )
    op.create_index("ix_accounting_exports_tenant_id", "accounting_exports", ["tenant_id"])

    op.create_table(
        "accounting_export_items",
        *base("accounting_export_items"),
        sa.Column("export_id", sa.Uuid(), nullable=False),
        sa.Column("doc_kind", sa.String(24), nullable=False),
        sa.Column("doc_id", sa.Uuid(), nullable=False),
        sa.Column("previously_exported_in", sa.Uuid(), nullable=True),
        tfk("export_id", "accounting_exports", "fk_accounting_export_items_export_id", "CASCADE"),
    )
    op.create_index("ix_accounting_export_items_tenant_id", "accounting_export_items", ["tenant_id"])
    op.create_index("ix_accounting_export_items_doc", "accounting_export_items", ["tenant_id", "doc_kind", "doc_id"])

    op.create_table(
        "reconciliation_runs",
        *base("reconciliation_runs"),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'running'"), nullable=False),
        sa.Column("findings_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.String(500), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_reconciliation_runs_tenant_id_id"),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_reconciliation_runs_status"),
    )
    op.create_index("ix_reconciliation_runs_tenant_id", "reconciliation_runs", ["tenant_id"])

    op.create_table(
        "reconciliation_findings",
        *base("reconciliation_findings"),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("check", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("subject_kind", sa.String(40), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        jsonb("details", "'{}'::jsonb"),
        sa.CheckConstraint("severity IN ('error', 'warning')", name="ck_reconciliation_findings_severity"),
        tfk("run_id", "reconciliation_runs", "fk_reconciliation_findings_run_id", "CASCADE"),
    )
    op.create_index("ix_reconciliation_findings_tenant_id", "reconciliation_findings", ["tenant_id"])
    op.create_index("ix_reconciliation_findings_run", "reconciliation_findings", ["tenant_id", "run_id"])

    op.create_table(
        "period_close_events",
        *base("period_close_events"),
        sa.Column("accounting_period_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        jsonb("checklist_snapshot", "'[]'::jsonb"),
        jsonb("acknowledged_warnings", "'[]'::jsonb"),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("request_status", sa.String(12), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "action IN ('close', 'reopen_requested', 'reopened', 'reclosed', 'reopen_rejected')", name="ck_period_close_events_action"
        ),
        sa.ForeignKeyConstraint(["accounting_period_id"], ["accounting_periods.id"], name="fk_period_close_events_period_id", ondelete="RESTRICT"),
        tfk("legal_entity_id", "legal_entities", "fk_period_close_events_le"),
    )
    op.create_index("ix_period_close_events_tenant_id", "period_close_events", ["tenant_id"])
    op.create_index("ix_period_close_events_period", "period_close_events", ["tenant_id", "accounting_period_id", "at"])

    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_rls(table)
        op.drop_table(table)
