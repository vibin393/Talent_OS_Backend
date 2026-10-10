# ruff: noqa: E501
"""Phase 2 foundation (Epic 13, stories 13.2 / 13.3): Legal Entities, GST registrations, numbering series,
accounting periods and FX rates (AD-17, AD-18, AD-22). Tenant-owned with forced RLS.

* ``business_units.default_legal_entity_id`` (nullable).
* Series configuration guard: ``prefix`` / ``fy_format`` / ``separator`` / ``pad`` cannot change once any
  counter of the series is > 0 (SQLSTATE ``TOS02``).
* Backfill (owner role): one active Legal Entity per tenant (``is_backfilled``), every BU's default entity,
  open accounting periods from the tenant's creation month through the current month + 2.

Revision ID: 20261002_200_02
Revises: 20261002_200_01
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261002_200_02"
down_revision: str | tuple[str, ...] | None = "20261002_200_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = (
    "legal_entities",
    "legal_entity_bank_accounts",
    "gst_registrations",
    "gst_registration_luts",
    "document_series",
    "document_series_counters",
    "accounting_periods",
    "fx_rates",
)

SERIES_GUARD = r"""
CREATE OR REPLACE FUNCTION tos_series_config_guard() RETURNS trigger LANGUAGE plpgsql
SET app.org_kind = 'internal' AS $fn$
BEGIN
    IF (NEW.prefix, NEW.fy_format, NEW.separator, NEW.pad)
           IS DISTINCT FROM (OLD.prefix, OLD.fy_format, OLD.separator, OLD.pad)
       AND EXISTS (SELECT 1 FROM document_series_counters c
                    WHERE c.tenant_id = OLD.tenant_id AND c.series_id = OLD.id AND c.last_value > 0) THEN
        RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:document_series';
    END IF;
    RETURN NEW;
END
$fn$
"""

BACKFILL_ENTITIES = """
INSERT INTO legal_entities (id, tenant_id, name, legal_name, default_currency, timezone, country, status, is_backfilled)
SELECT gen_random_uuid(), t.id, t.name, t.name,
       CASE WHEN EXISTS (SELECT 1 FROM currencies c WHERE c.code = upper(t.currency) AND c.document_allowed)
            THEN upper(t.currency) ELSE 'INR' END,
       t.timezone, 'IN', 'active', true
  FROM tenants t
 WHERE t.status <> 'deleted'
   AND NOT EXISTS (SELECT 1 FROM legal_entities le WHERE le.tenant_id = t.id)
"""

BACKFILL_BUS = """
UPDATE business_units b
   SET default_legal_entity_id = le.id
  FROM legal_entities le
 WHERE le.tenant_id = b.tenant_id AND le.is_backfilled AND b.default_legal_entity_id IS NULL
"""

BACKFILL_PERIODS = """
INSERT INTO accounting_periods (id, tenant_id, legal_entity_id, year, month, status)
SELECT gen_random_uuid(), le.tenant_id, le.id, extract(year FROM m)::int, extract(month FROM m)::int, 'open'
  FROM legal_entities le
  JOIN tenants t ON t.id = le.tenant_id
 CROSS JOIN LATERAL generate_series(
       date_trunc('month', t.created_at AT TIME ZONE le.timezone),
       date_trunc('month', now() AT TIME ZONE le.timezone) + interval '2 months',
       interval '1 month') AS m
 WHERE le.is_backfilled
ON CONFLICT (legal_entity_id, year, month) DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "legal_entities",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("legal_name", sa.String(length=300), nullable=False),
        sa.Column("pan", sa.CHAR(length=10), nullable=True),
        sa.Column("cin", sa.String(length=21), nullable=True),
        sa.Column(
            "address",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("state_code", sa.CHAR(length=2), nullable=True),
        sa.Column("country", sa.CHAR(length=2), server_default=sa.text("'IN'"), nullable=False),
        sa.Column("default_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("timezone", sa.String(length=64), server_default=sa.text("'Asia/Kolkata'"), nullable=False),
        sa.Column("logo_file_id", sa.Uuid(), nullable=True),
        sa.Column("signatory_name", sa.String(length=200), nullable=True),
        sa.Column("signatory_title", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("is_backfilled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "pan IS NULL OR pan ~ '^[A-Z]{5}[0-9]{4}[A-Z]$'", name=op.f("ck_legal_entities_pan_format")
        ),
        sa.CheckConstraint("status IN ('active', 'inactive')", name=op.f("ck_legal_entities_status")),
        sa.ForeignKeyConstraint(
            ["default_currency"],
            ["currencies.code"],
            name=op.f("fk_legal_entities_default_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["state_code"],
            ["gst_states.code"],
            name=op.f("fk_legal_entities_state_code_gst_states"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "logo_file_id"],
            ["stored_files.tenant_id", "stored_files.id"],
            name="fk_legal_entities_logo_file_id_stored_files",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_legal_entities_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_legal_entities")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_legal_entities_tenant_id_id")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_legal_entities_tenant_id_name")),
    )
    op.create_index(op.f("ix_legal_entities_tenant_id"), "legal_entities", ["tenant_id"], unique=False)
    op.create_table(
        "legal_entity_bank_accounts",
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("bank_name", sa.String(length=200), nullable=False),
        sa.Column("account_number", sa.Text(), nullable=False),
        sa.Column("account_number_last4", sa.String(length=4), nullable=False),
        sa.Column("account_number_bidx", sa.String(length=32), nullable=False),
        sa.Column("ifsc", sa.String(length=11), nullable=True),
        sa.Column("swift", sa.String(length=11), nullable=True),
        sa.Column("currency", sa.CHAR(length=3), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["currency"],
            ["currencies.code"],
            name=op.f("fk_legal_entity_bank_accounts_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_legal_entity_bank_accounts_legal_entity_id_legal_entities",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_legal_entity_bank_accounts_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_legal_entity_bank_accounts")),
    )
    op.create_index(
        "ix_legal_entity_bank_accounts_legal_entity_id",
        "legal_entity_bank_accounts",
        ["legal_entity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_legal_entity_bank_accounts_tenant_id"),
        "legal_entity_bank_accounts",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "uq_legal_entity_bank_accounts_default",
        "legal_entity_bank_accounts",
        ["legal_entity_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_table(
        "gst_registrations",
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("gstin", sa.CHAR(length=15), nullable=False),
        sa.Column("state_code", sa.CHAR(length=2), nullable=False),
        sa.Column(
            "address",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_principal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("einvoice_applicable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("irp_reporting_limit", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('active', 'inactive')", name=op.f("ck_gst_registrations_status")),
        sa.CheckConstraint(
            "state_code = substr(gstin, 1, 2)", name=op.f("ck_gst_registrations_state_matches_gstin")
        ),
        sa.ForeignKeyConstraint(
            ["state_code"],
            ["gst_states.code"],
            name=op.f("fk_gst_registrations_state_code_gst_states"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_gst_registrations_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_gst_registrations_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gst_registrations")),
        sa.UniqueConstraint("tenant_id", "gstin", name=op.f("uq_gst_registrations_tenant_id_gstin")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_gst_registrations_tenant_id_id")),
    )
    op.create_index(
        "ix_gst_registrations_legal_entity_id", "gst_registrations", ["legal_entity_id"], unique=False
    )
    op.create_index(op.f("ix_gst_registrations_tenant_id"), "gst_registrations", ["tenant_id"], unique=False)
    op.create_index(
        "uq_gst_registrations_principal",
        "gst_registrations",
        ["legal_entity_id"],
        unique=True,
        postgresql_where=sa.text("is_principal"),
    )
    op.create_table(
        "gst_registration_luts",
        sa.Column("gst_registration_id", sa.Uuid(), nullable=False),
        sa.Column("fiscal_year", sa.CHAR(length=5), nullable=False),
        sa.Column("lut_number", sa.String(length=50), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "fiscal_year ~ '^[0-9]{2}-[0-9]{2}$'", name=op.f("ck_gst_registration_luts_fiscal_year")
        ),
        sa.CheckConstraint("valid_to >= valid_from", name=op.f("ck_gst_registration_luts_validity")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "gst_registration_id"],
            ["gst_registrations.tenant_id", "gst_registrations.id"],
            name="fk_gst_registration_luts_gst_registration_id_gst_registrations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_gst_registration_luts_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gst_registration_luts")),
        sa.UniqueConstraint(
            "gst_registration_id",
            "fiscal_year",
            name=op.f("uq_gst_registration_luts_gst_registration_id_fiscal_year"),
        ),
    )
    op.create_index(
        op.f("ix_gst_registration_luts_tenant_id"), "gst_registration_luts", ["tenant_id"], unique=False
    )
    op.create_table(
        "document_series",
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("gst_registration_id", sa.Uuid(), nullable=True),
        sa.Column("doc_type", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=10), server_default=sa.text("''"), nullable=False),
        sa.Column("fy_format", sa.String(length=8), server_default=sa.text("'YY-YY'"), nullable=False),
        sa.Column("separator", sa.String(length=1), server_default=sa.text("'/'"), nullable=False),
        sa.Column("pad", sa.SmallInteger(), server_default=sa.text("4"), nullable=False),
        sa.Column("is_legacy", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("allow_backdated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "doc_type IN ('invoice', 'credit_note', 'debit_note')", name=op.f("ck_document_series_doc_type")
        ),
        sa.CheckConstraint(
            "fy_format IN ('YY-YY', 'YYYY-YY', 'YY', 'none')", name=op.f("ck_document_series_fy_format")
        ),
        sa.CheckConstraint("prefix ~ '^[A-Za-z0-9/-]*$'", name=op.f("ck_document_series_prefix_chars")),
        sa.CheckConstraint("separator IN ('/', '-', '')", name=op.f("ck_document_series_separator")),
        sa.CheckConstraint("status IN ('active', 'inactive')", name=op.f("ck_document_series_status")),
        sa.CheckConstraint("pad BETWEEN 1 AND 10", name=op.f("ck_document_series_pad")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "gst_registration_id"],
            ["gst_registrations.tenant_id", "gst_registrations.id"],
            name="fk_document_series_gst_registration_id_gst_registrations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_document_series_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_document_series_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_series")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_document_series_tenant_id_id")),
    )
    op.create_index(
        "ix_document_series_legal_entity_id", "document_series", ["legal_entity_id"], unique=False
    )
    op.create_index(op.f("ix_document_series_tenant_id"), "document_series", ["tenant_id"], unique=False)
    op.create_index(
        "uq_document_series_active",
        "document_series",
        ["tenant_id", "gst_registration_id", "doc_type"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND NOT is_legacy"),
    )
    op.create_table(
        "document_series_counters",
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("fiscal_year", sa.CHAR(length=5), nullable=False),
        sa.Column("last_value", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_doc_date", sa.Date(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("last_value >= 0", name=op.f("ck_document_series_counters_last_value")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "series_id"],
            ["document_series.tenant_id", "document_series.id"],
            name="fk_document_series_counters_series_id_document_series",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_document_series_counters_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("series_id", "fiscal_year", name=op.f("pk_document_series_counters")),
    )
    op.create_index(
        op.f("ix_document_series_counters_tenant_id"), "document_series_counters", ["tenant_id"], unique=False
    )
    op.create_table(
        "accounting_periods",
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("month", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'open'"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.Uuid(), nullable=True),
        sa.Column("reopened_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_accounting_periods_status")),
        sa.CheckConstraint("month BETWEEN 1 AND 12", name=op.f("ck_accounting_periods_month")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_accounting_periods_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_accounting_periods_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounting_periods")),
        sa.UniqueConstraint(
            "legal_entity_id", "year", "month", name=op.f("uq_accounting_periods_legal_entity_id_year_month")
        ),
    )
    op.create_index(
        op.f("ix_accounting_periods_tenant_id"), "accounting_periods", ["tenant_id"], unique=False
    )
    op.create_table(
        "fx_rates",
        sa.Column("from_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("to_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("rate", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source <> ''", name=op.f("ck_fx_rates_source_required")),
        sa.CheckConstraint("from_currency <> to_currency", name=op.f("ck_fx_rates_distinct_currencies")),
        sa.CheckConstraint("rate > 0", name=op.f("ck_fx_rates_rate_positive")),
        sa.ForeignKeyConstraint(
            ["from_currency"],
            ["currencies.code"],
            name=op.f("fk_fx_rates_from_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_fx_rates_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["to_currency"],
            ["currencies.code"],
            name=op.f("fk_fx_rates_to_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fx_rates")),
        sa.UniqueConstraint(
            "tenant_id",
            "from_currency",
            "to_currency",
            "rate_date",
            name=op.f("uq_fx_rates_tenant_id_from_currency_to_currency_rate_date"),
        ),
    )
    op.create_index(op.f("ix_fx_rates_tenant_id"), "fx_rates", ["tenant_id"], unique=False)
    for table in TABLES:
        enable_rls(table)

    op.add_column("business_units", sa.Column("default_legal_entity_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_business_units_default_legal_entity_id_legal_entities",
        "business_units",
        "legal_entities",
        ["tenant_id", "default_legal_entity_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )

    op.execute(SERIES_GUARD)
    op.execute(
        "CREATE TRIGGER tos_series_config_guard BEFORE UPDATE ON document_series "
        "FOR EACH ROW EXECUTE FUNCTION tos_series_config_guard()"
    )

    op.execute(BACKFILL_ENTITIES)
    op.execute(BACKFILL_BUS)
    op.execute(BACKFILL_PERIODS)


def downgrade() -> None:
    op.drop_constraint(
        "fk_business_units_default_legal_entity_id_legal_entities", "business_units", type_="foreignkey"
    )
    op.drop_column("business_units", "default_legal_entity_id")
    op.execute("DROP TRIGGER IF EXISTS tos_series_config_guard ON document_series")
    op.execute("DROP FUNCTION IF EXISTS tos_series_config_guard()")
    for table in reversed(TABLES):
        disable_rls(table)
        op.drop_table(table)
