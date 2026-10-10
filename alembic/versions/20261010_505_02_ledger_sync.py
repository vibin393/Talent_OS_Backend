# ruff: noqa: E501
"""Epic 71 (HRMS & Accounting Connectors, stories 71.4-71.6, spec A2): ``ledger_mappings``, ``ledger_sync_records``,
``ledger_runs``, ``payment_suggestions`` and ``ledger_settings``. Tenant tables with RLS + org RLS (internal only).

Revision ID: 20261010_505_02_ledger_sync
Revises: 20261010_505_01_hrms_sync
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_505_02_ledger_sync"
down_revision: str | tuple[str, ...] | None = "20261010_505_01_hrms_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("ledger_mappings", "ledger_sync_records", "ledger_runs", "payment_suggestions", "ledger_settings")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _base(t: str) -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


def upgrade() -> None:
    t = "ledger_mappings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("external_ref", sa.String(200), nullable=False),
        sa.Column("external_label", sa.String(200), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "installation_id", "legal_entity_id", "kind", "key", name="uq_ledger_mappings_key"),
        _fk("installation_id", "app_installations", "fk_ledger_mappings_installation", "CASCADE"),
        _fk("legal_entity_id", "legal_entities", "fk_ledger_mappings_legal_entity"),
        sa.CheckConstraint(
            _in("kind", ("revenue", "gst_output", "tds_receivable", "round_off", "customer", "item", "bank_account", "discount")),
            name=op.f("ck_ledger_mappings_kind"),
        ),
    )

    t = "ledger_sync_records"
    op.create_table(
        t,
        *_base(t),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("document_kind", sa.String(12), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("synced_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(14), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("external_ref", sa.String(200), nullable=True),
        sa.Column("external_number", sa.String(60), nullable=True),
        sa.Column("message", sa.String(500), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skipped_by", sa.Uuid(), nullable=True),
        sa.Column("skip_reason", sa.String(200), nullable=True),
        sa.Column("tally_run_id", sa.Uuid(), nullable=True),
        sa.Column("doc_date", sa.Date(), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "installation_id", "document_kind", "document_id", name="uq_ledger_sync_records_doc"),
        _fk("installation_id", "app_installations", "fk_ledger_sync_records_installation", "CASCADE"),
        sa.CheckConstraint(_in("document_kind", ("invoice", "credit_note", "debit_note", "payment")), name=op.f("ck_ledger_sync_records_document_kind")),
        sa.CheckConstraint(
            _in("status", ("pending", "synced", "failed", "skipped", "out_of_sync", "blocked_period")), name=op.f("ck_ledger_sync_records_status")
        ),
    )
    op.create_index("ix_ledger_sync_records_status_date", t, ["status", "doc_date"])
    op.create_index("ix_ledger_sync_records_entity_date", t, ["legal_entity_id", "doc_date"])

    t = "ledger_runs"
    op.create_table(
        t,
        *_base(t),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=True),
        sa.Column("to_date", sa.Date(), nullable=True),
        sa.Column("include_masters", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("counts", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        _fk("installation_id", "app_installations", "fk_ledger_runs_installation", "CASCADE"),
        _fk("legal_entity_id", "legal_entities", "fk_ledger_runs_legal_entity"),
        sa.CheckConstraint(_in("kind", ("push", "tally_file", "pull_payments", "status", "masters")), name=op.f("ck_ledger_runs_kind")),
        sa.CheckConstraint(_in("status", ("queued", "running", "completed", "failed")), name=op.f("ck_ledger_runs_status")),
    )
    op.create_index("ix_ledger_runs_installation_created", t, ["installation_id", sa.text("created_at DESC")])

    t = "payment_suggestions"
    op.create_table(
        t,
        *_base(t),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("external_ref", sa.String(200), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("invoice_number", sa.String(60), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("tds_amount", sa.Numeric(16, 2), nullable=True),
        sa.Column("mode", sa.String(10), nullable=True),
        sa.Column("reference", sa.String(120), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.String(500), nullable=True),
        sa.Column("match_note", sa.String(200), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "installation_id", "external_ref", name="uq_payment_suggestions_ref"),
        _fk("installation_id", "app_installations", "fk_payment_suggestions_installation", "CASCADE"),
        sa.CheckConstraint(_in("status", ("pending", "confirmed", "rejected", "stale")), name=op.f("ck_payment_suggestions_status")),
        sa.CheckConstraint(
            "match_note IS NULL OR " + _in("match_note", ("invoice_not_found", "currency_mismatch", "over_payment")),
            name=op.f("ck_payment_suggestions_match_note"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payment_suggestions_amount")),
    )
    op.create_index("ix_payment_suggestions_status_paid_on", t, ["status", "paid_on"])

    t = "ledger_settings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("auto_push_on_issue", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("push_payments", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("pull_payments_every_minutes", sa.Integer(), server_default=sa.text("60"), nullable=False),
        sa.Column("suggestion_stale_days", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", name="uq_ledger_settings_tenant_id"),
        sa.CheckConstraint("pull_payments_every_minutes >= 30", name=op.f("ck_ledger_settings_pull_every")),
        sa.CheckConstraint("suggestion_stale_days BETWEEN 1 AND 365", name=op.f("ck_ledger_settings_stale_days")),
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
