# ruff: noqa: E501
"""Phase 2 foundation (Epic 13, stories 13.8 / 13.9 / 13.11): posting ledgers, idempotency keys, file import
batches, the provider connector framework and retention classes.

* ``billing_postings`` / ``payable_postings`` (AD-23): immutable except ``state`` / ``posted_at`` /
  ``released_at`` / ``release_reason``; a state guard allows ``reserved -> posted|released`` and
  ``posted -> released`` only and refuses DELETE. Internal-only org RLS.
* ``idempotency_keys``, ``file_import_batches`` (the spec's ``import_batches``; that name is taken by the
  Phase 1 candidate import), ``provider_connector_configs``, ``connector_jobs``, ``connector_inbound_events``:
  tenant RLS + internal-only org RLS. ``connector_hook_lookups`` is a platform table.
* ``retention_classes`` (AD-34) + per-tenant seeds (financial, signed agreements, timesheets, BGV evidence).

Revision ID: 20261002_200_06
Revises: 20261002_200_05
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    disable_immutability,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261002_200_06"
down_revision: str | tuple[str, ...] | None = "20261002_200_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEDGERS: tuple[str, ...] = ("billing_postings", "payable_postings")
INTERNAL_TABLES: tuple[str, ...] = (
    "billing_postings",
    "payable_postings",
    "idempotency_keys",
    "file_import_batches",
    "provider_connector_configs",
    "connector_jobs",
    "connector_inbound_events",
)
TENANT_TABLES: tuple[str, ...] = (*INTERNAL_TABLES, "retention_classes")

STATE_GUARD = r"""
CREATE OR REPLACE FUNCTION tos_posting_state_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:' || TG_TABLE_NAME;
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state
       AND NOT ((OLD.state = 'reserved' AND NEW.state IN ('posted', 'released'))
                OR (OLD.state = 'posted' AND NEW.state = 'released')) THEN
        RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:' || TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END
$fn$
"""

RETENTION_SEEDS = """
INSERT INTO retention_classes (id, tenant_id, key, label, basis, min_retention_months, delete_after_months, is_default)
SELECT gen_random_uuid(), t.id, s.key, s.label, s.basis, s.min_months, s.delete_after, true
  FROM tenants t
 CROSS JOIN (VALUES
     ('financial', 'Financial records', 'document_date', 96, NULL::int),
     ('signed_agreements', 'Signed agreements', 'end_of_engagement', 96, NULL::int),
     ('timesheets', 'Timesheets', 'document_date', 96, NULL::int),
     ('bgv_evidence', 'Background verification evidence', 'adjudication', 0, 12)
 ) AS s(key, label, basis, min_months, delete_after)
 WHERE t.status <> 'deleted'
ON CONFLICT (tenant_id, key) DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "billing_postings",
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("amount", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=200), nullable=True),
        sa.Column("created_by_kind", sa.String(length=20), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "state IN ('reserved', 'posted', 'released')", name=op.f("ck_billing_postings_state")
        ),
        sa.CheckConstraint(
            "target_kind IN ('invoice_line', 'credit_note_line', 'debit_note_line')",
            name=op.f("ck_billing_postings_target_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_billing_postings_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_postings")),
    )
    op.create_index("ix_billing_postings_document_id", "billing_postings", ["document_id"], unique=False)
    op.create_index(
        "ix_billing_postings_source", "billing_postings", ["source_kind", "source_id"], unique=False
    )
    op.create_index(
        "ix_billing_postings_target", "billing_postings", ["target_kind", "target_id"], unique=False
    )
    op.create_index(op.f("ix_billing_postings_tenant_id"), "billing_postings", ["tenant_id"], unique=False)
    op.create_index(
        "uq_billing_postings_active",
        "billing_postings",
        ["tenant_id", "source_kind", "source_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'released'"),
    )
    op.create_table(
        "payable_postings",
        sa.Column("payee_kind", sa.String(length=10), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("amount", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=200), nullable=True),
        sa.Column("created_by_kind", sa.String(length=20), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("payee_kind IN ('worker', 'vendor')", name=op.f("ck_payable_postings_payee_kind")),
        sa.CheckConstraint(
            "state IN ('reserved', 'posted', 'released')", name=op.f("ck_payable_postings_state")
        ),
        sa.CheckConstraint(
            "target_kind IN ('payroll_row', 'vendor_statement_line')",
            name=op.f("ck_payable_postings_target_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_payable_postings_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payable_postings")),
    )
    op.create_index("ix_payable_postings_document_id", "payable_postings", ["document_id"], unique=False)
    op.create_index(
        "ix_payable_postings_source", "payable_postings", ["source_kind", "source_id"], unique=False
    )
    op.create_index(
        "ix_payable_postings_target", "payable_postings", ["target_kind", "target_id"], unique=False
    )
    op.create_index(op.f("ix_payable_postings_tenant_id"), "payable_postings", ["tenant_id"], unique=False)
    op.create_index(
        "uq_payable_postings_active",
        "payable_postings",
        ["tenant_id", "source_kind", "source_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'released'"),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("principal_type", sa.String(length=16), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_idempotency_keys_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_keys")),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_type",
            "principal_id",
            "key",
            name=op.f("uq_idempotency_keys_tenant_id_principal_type_principal_id_key"),
        ),
    )
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"], unique=False)
    op.create_index(op.f("ix_idempotency_keys_tenant_id"), "idempotency_keys", ["tenant_id"], unique=False)
    op.create_table(
        "file_import_batches",
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'previewed'"), nullable=False),
        sa.Column("row_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('previewed', 'applied', 'failed', 'discarded')",
            name=op.f("ck_file_import_batches_status"),
        ),
        sa.CheckConstraint("row_count >= 0 AND error_count >= 0", name=op.f("ck_file_import_batches_counts")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "file_id"],
            ["stored_files.tenant_id", "stored_files.id"],
            name="fk_file_import_batches_file_id_stored_files",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_file_import_batches_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_import_batches")),
    )
    op.create_index(
        op.f("ix_file_import_batches_tenant_id"), "file_import_batches", ["tenant_id"], unique=False
    )
    op.create_index(
        "uq_file_import_batches_applied",
        "file_import_batches",
        ["tenant_id", "kind", "sha256"],
        unique=True,
        postgresql_where=sa.text("status = 'applied' AND (summary ->> 'forced_reapply_of') IS NULL"),
    )
    op.create_table(
        "provider_connector_configs",
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("provider_key", sa.String(length=40), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("credentials", sa.Text(), nullable=True),
        sa.Column("webhook_secret", sa.Text(), nullable=True),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "category IN ('esign', 'bgv', 'einvoice', 'payroll', 'accounting', 'sms', 'whatsapp')",
            name=op.f("ck_provider_connector_configs_category"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name=op.f("ck_provider_connector_configs_status")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_provider_connector_configs_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_provider_connector_configs_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_connector_configs")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_provider_connector_configs_tenant_id_id")),
    )
    op.create_index(
        op.f("ix_provider_connector_configs_tenant_id"),
        "provider_connector_configs",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "uq_provider_connector_configs_active",
        "provider_connector_configs",
        ["tenant_id", "category", "legal_entity_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "connector_jobs",
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("op", sa.String(length=60), nullable=False),
        sa.Column("subject_kind", sa.String(length=40), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("config_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "category IN ('esign', 'bgv', 'einvoice', 'payroll', 'accounting', 'sms', 'whatsapp')",
            name=op.f("ck_connector_jobs_category"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'dead')",
            name=op.f("ck_connector_jobs_status"),
        ),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1", name=op.f("ck_connector_jobs_attempts")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_connector_jobs_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_jobs")),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name=op.f("uq_connector_jobs_tenant_id_idempotency_key")
        ),
    )
    op.create_index("ix_connector_jobs_status", "connector_jobs", ["status", "next_attempt_at"], unique=False)
    op.create_index(
        "ix_connector_jobs_subject", "connector_jobs", ["subject_kind", "subject_id"], unique=False
    )
    op.create_index(op.f("ix_connector_jobs_tenant_id"), "connector_jobs", ["tenant_id"], unique=False)
    op.create_table(
        "connector_inbound_events",
        sa.Column("config_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'received'"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "category IN ('esign', 'bgv', 'einvoice', 'payroll', 'accounting', 'sms', 'whatsapp')",
            name=op.f("ck_connector_inbound_events_category"),
        ),
        sa.CheckConstraint(
            "status IN ('received', 'processed', 'failed')", name=op.f("ck_connector_inbound_events_status")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "config_id"],
            ["provider_connector_configs.tenant_id", "provider_connector_configs.id"],
            name="fk_connector_inbound_events_config_id_configs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_connector_inbound_events_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_inbound_events")),
    )
    op.create_index(
        "ix_connector_inbound_events_status",
        "connector_inbound_events",
        ["status", "received_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_connector_inbound_events_tenant_id"), "connector_inbound_events", ["tenant_id"], unique=False
    )
    op.create_table(
        "connector_hook_lookups",
        sa.Column("config_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_connector_hook_lookups_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("config_id", name=op.f("pk_connector_hook_lookups")),
    )
    op.create_table(
        "retention_classes",
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("basis", sa.String(length=20), nullable=False),
        sa.Column("min_retention_months", sa.Integer(), nullable=False),
        sa.Column("delete_after_months", sa.Integer(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "basis IN ('document_date', 'adjudication', 'end_of_engagement')",
            name=op.f("ck_retention_classes_basis"),
        ),
        sa.CheckConstraint(
            "delete_after_months IS NULL OR delete_after_months >= min_retention_months",
            name=op.f("ck_retention_classes_delete_after"),
        ),
        sa.CheckConstraint(
            "min_retention_months >= 0", name=op.f("ck_retention_classes_min_retention_months")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_retention_classes_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retention_classes")),
        sa.UniqueConstraint("tenant_id", "key", name=op.f("uq_retention_classes_tenant_id_key")),
    )
    op.create_index(op.f("ix_retention_classes_tenant_id"), "retention_classes", ["tenant_id"], unique=False)
    for table in TENANT_TABLES:
        enable_rls(table)
    for table in INTERNAL_TABLES:
        enable_org_rls(table)

    op.execute(STATE_GUARD)
    for table in LEDGERS:
        enable_immutability(
            table, status_column=None, allowed_columns=("state", "posted_at", "released_at", "release_reason")
        )
        op.execute(
            f"CREATE TRIGGER tos_posting_state_guard BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION tos_posting_state_guard()"
        )

    op.execute(RETENTION_SEEDS)


def downgrade() -> None:
    for table in LEDGERS:
        op.execute(f"DROP TRIGGER IF EXISTS tos_posting_state_guard ON {table}")
        disable_immutability(table)
    op.execute("DROP FUNCTION IF EXISTS tos_posting_state_guard()")
    op.drop_table("connector_hook_lookups")
    for table in INTERNAL_TABLES:
        disable_org_rls(table)
    for table in reversed(TENANT_TABLES):
        disable_rls(table)
        op.drop_table(table)
