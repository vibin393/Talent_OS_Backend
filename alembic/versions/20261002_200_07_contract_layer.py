# ruff: noqa: E501
"""Phase 2 foundation (Epic 13, story 13.10): the contract layer hub tables (AD-36).

``contracts``, ``purchase_orders``, ``placements``, ``rate_segments``, ``invoices``, ``debit_notes`` -- keys, org
columns, status and Legal Entity columns only; epic branches add the rest. Every Phase 2 epic branch forks from
this revision (``depends_on`` / ``down_revision`` = ``20261002_200_07``).

Guards: placements' parties are immutable; rate segments never overlap (EXCLUDE gist) and only
``effective_to`` may change; invoices / debit notes are immutable once issued (``issued -> cancelled`` may set
``cancel_basis`` / ``cancelled_at``) and period-guarded on ``doc_date``. Org RLS on all six.

Revision ID: 20261002_200_07
Revises: 20261002_200_06
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import (
    InheritRef,
    disable_immutability,
    disable_org_rls,
    disable_period_guard,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_period_guard,
    enable_rls,
)

revision: str = "20261002_200_07"
down_revision: str | tuple[str, ...] | None = "20261002_200_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = (
    "contracts",
    "purchase_orders",
    "placements",
    "rate_segments",
    "invoices",
    "debit_notes",
)
DOCUMENTS: tuple[str, ...] = ("invoices", "debit_notes")

PLACEMENT_PARTIES_GUARD = r"""
CREATE OR REPLACE FUNCTION tos_placement_parties_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.client_id IS DISTINCT FROM OLD.client_id
       OR NEW.vendor_id IS DISTINCT FROM OLD.vendor_id
       OR NEW.candidate_id IS DISTINCT FROM OLD.candidate_id THEN
        RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:placements';
    END IF;
    RETURN NEW;
END
$fn$
"""


def upgrade() -> None:
    op.create_table(
        "contracts",
        sa.Column("human_id", sa.String(length=20), nullable=False),
        sa.Column("contract_type", sa.String(length=20), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("parent_contract_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "contract_type IN ('msa', 'sow', 'amendment', 'vendor_agreement')",
            name=op.f("ck_contracts_contract_type"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'active', 'expired', 'terminated')",
            name=op.f("ck_contracts_status"),
        ),
        sa.CheckConstraint("(client_id IS NULL) <> (vendor_id IS NULL)", name=op.f("ck_contracts_one_party")),
        sa.CheckConstraint("end_date IS NULL OR end_date >= start_date", name=op.f("ck_contracts_dates")),
        sa.ForeignKeyConstraint(
            ["currency"],
            ["currencies.code"],
            name=op.f("fk_contracts_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_contracts_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_contracts_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_contract_id"],
            ["contracts.tenant_id", "contracts.id"],
            name="fk_contracts_parent_contract_id_contracts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "vendor_id"],
            ["vendors.tenant_id", "vendors.id"],
            name="fk_contracts_vendor_id_vendors",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_contracts_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contracts")),
        sa.UniqueConstraint("tenant_id", "human_id", name=op.f("uq_contracts_tenant_id_human_id")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_contracts_tenant_id_id")),
    )
    op.create_index("ix_contracts_client_id", "contracts", ["client_id"], unique=False)
    op.create_index(op.f("ix_contracts_tenant_id"), "contracts", ["tenant_id"], unique=False)
    op.create_index("ix_contracts_vendor_id", "contracts", ["vendor_id"], unique=False)
    op.create_table(
        "purchase_orders",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=True),
        sa.Column("po_number", sa.String(length=60), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("value_cap", sa.Numeric(precision=16, scale=2), nullable=True),
        sa.Column("hours_cap", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'closed', 'cancelled')", name=op.f("ck_purchase_orders_status")
        ),
        sa.CheckConstraint("hours_cap IS NULL OR hours_cap > 0", name=op.f("ck_purchase_orders_hours_cap")),
        sa.CheckConstraint("valid_to >= valid_from", name=op.f("ck_purchase_orders_validity")),
        sa.CheckConstraint("value_cap IS NULL OR value_cap > 0", name=op.f("ck_purchase_orders_value_cap")),
        sa.ForeignKeyConstraint(
            ["currency"],
            ["currencies.code"],
            name=op.f("fk_purchase_orders_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_purchase_orders_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "contract_id"],
            ["contracts.tenant_id", "contracts.id"],
            name="fk_purchase_orders_contract_id_contracts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_purchase_orders_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_purchase_orders_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_orders")),
        sa.UniqueConstraint(
            "tenant_id",
            "client_id",
            "po_number",
            name=op.f("uq_purchase_orders_tenant_id_client_id_po_number"),
        ),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_purchase_orders_tenant_id_id")),
    )
    op.create_index(op.f("ix_purchase_orders_tenant_id"), "purchase_orders", ["tenant_id"], unique=False)
    op.create_table(
        "placements",
        sa.Column("human_id", sa.String(length=20), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("offer_id", sa.Uuid(), nullable=True),
        sa.Column("demand_id", sa.Uuid(), nullable=True),
        sa.Column("contract_id", sa.Uuid(), nullable=True),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("supplier_gst_registration_id", sa.Uuid(), nullable=True),
        sa.Column("worker_type", sa.String(length=20), nullable=False),
        sa.Column("payee_kind", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'pre_joining'"), nullable=False),
        sa.Column("planned_start_date", sa.Date(), nullable=False),
        sa.Column("planned_end_date", sa.Date(), nullable=True),
        sa.Column("actual_start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("work_state_code", sa.CHAR(length=2), nullable=True),
        sa.Column(
            "work_timezone", sa.String(length=64), server_default=sa.text("'Asia/Kolkata'"), nullable=False
        ),
        sa.Column("account_manager_user_id", sa.Uuid(), nullable=True),
        sa.Column("recruiter_user_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(payee_kind = 'vendor') = (vendor_id IS NOT NULL)", name=op.f("ck_placements_vendor_payee")
        ),
        sa.CheckConstraint("payee_kind IN ('worker', 'vendor')", name=op.f("ck_placements_payee_kind")),
        sa.CheckConstraint(
            "status IN ('pre_joining', 'joining_confirmed', 'active', 'offboarding', 'ended', 'dropped')",
            name=op.f("ck_placements_status"),
        ),
        sa.CheckConstraint(
            "worker_type IN ('employee', 'contractor', 'vendor_contractor')",
            name=op.f("ck_placements_worker_type"),
        ),
        sa.ForeignKeyConstraint(
            ["currency"],
            ["currencies.code"],
            name=op.f("fk_placements_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_manager_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_placements_account_manager_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            name="fk_placements_application_id_applications",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_placements_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_placements_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "contract_id"],
            ["contracts.tenant_id", "contracts.id"],
            name="fk_placements_contract_id_contracts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_placements_demand_id_demands",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_placements_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "offer_id"],
            ["offers.tenant_id", "offers.id"],
            name="fk_placements_offer_id_offers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "recruiter_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_placements_recruiter_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "supplier_gst_registration_id"],
            ["gst_registrations.tenant_id", "gst_registrations.id"],
            name="fk_placements_supplier_gst_registration_id_gst_registrations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "vendor_id"],
            ["vendors.tenant_id", "vendors.id"],
            name="fk_placements_vendor_id_vendors",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_placements_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["work_state_code"],
            ["gst_states.code"],
            name=op.f("fk_placements_work_state_code_gst_states"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_placements")),
        sa.UniqueConstraint("tenant_id", "human_id", name=op.f("uq_placements_tenant_id_human_id")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_placements_tenant_id_id")),
        sa.UniqueConstraint("tenant_id", "offer_id", name=op.f("uq_placements_tenant_id_offer_id")),
    )
    op.create_index("ix_placements_candidate_id", "placements", ["candidate_id"], unique=False)
    op.create_index("ix_placements_client_id", "placements", ["client_id"], unique=False)
    op.create_index("ix_placements_demand_id", "placements", ["demand_id"], unique=False)
    op.create_index("ix_placements_legal_entity_id", "placements", ["legal_entity_id"], unique=False)
    op.create_index(op.f("ix_placements_tenant_id"), "placements", ["tenant_id"], unique=False)
    op.create_index("ix_placements_vendor_id", "placements", ["vendor_id"], unique=False)
    op.create_table(
        "rate_segments",
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("rate_unit", sa.String(length=10), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("pay_rate", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("bill_rate", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("vendor_rate", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "rate_unit IN ('hour', 'day', 'month', 'fixed')", name=op.f("ck_rate_segments_rate_unit")
        ),
        sa.CheckConstraint(
            "(pay_rate IS NULL OR pay_rate >= 0) AND (bill_rate IS NULL OR bill_rate >= 0) AND (vendor_rate IS NULL OR vendor_rate >= 0)",
            name=op.f("ck_rate_segments_rates"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name=op.f("ck_rate_segments_dates")
        ),
        sa.ForeignKeyConstraint(
            ["currency"],
            ["currencies.code"],
            name=op.f("fk_rate_segments_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "placement_id"],
            ["placements.tenant_id", "placements.id"],
            name="fk_rate_segments_placement_id_placements",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_rate_segments_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_segments")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_rate_segments_tenant_id_id")),
    )
    op.create_index(
        "ix_rate_segments_placement_id", "rate_segments", ["placement_id", "effective_from"], unique=False
    )
    op.create_index(op.f("ix_rate_segments_tenant_id"), "rate_segments", ["tenant_id"], unique=False)
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("supplier_gst_registration_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("series_id", sa.Uuid(), nullable=True),
        sa.Column("fiscal_year", sa.CHAR(length=5), nullable=True),
        sa.Column("number", sa.String(length=16), nullable=True),
        sa.Column("doc_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column(
            "document_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("is_imported", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("cancel_basis", sa.String(length=20), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "cancel_basis IS NULL OR cancel_basis IN ('full_credit_note', 'irn_cancelled')",
            name=op.f("ck_invoices_cancel_basis"),
        ),
        sa.CheckConstraint(
            "number IS NULL OR number ~ '^[A-Za-z0-9/-]{1,16}$'", name=op.f("ck_invoices_number_format")
        ),
        sa.CheckConstraint(
            "status <> 'cancelled' OR cancel_basis IS NOT NULL",
            name=op.f("ck_invoices_cancel_basis_required"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'issued', 'discarded', 'cancelled')",
            name=op.f("ck_invoices_status"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('issued', 'cancelled') OR (number IS NOT NULL AND doc_date IS NOT NULL)",
            name=op.f("ck_invoices_issued_has_number"),
        ),
        sa.ForeignKeyConstraint(
            ["currency"],
            ["currencies.code"],
            name=op.f("fk_invoices_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_invoices_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_invoices_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "series_id"],
            ["document_series.tenant_id", "document_series.id"],
            name="fk_invoices_series_id_document_series",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "supplier_gst_registration_id"],
            ["gst_registrations.tenant_id", "gst_registrations.id"],
            name="fk_invoices_supplier_gst_registration_id_gst_registrations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_invoices_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_invoices_tenant_id_id")),
    )
    op.create_index("ix_invoices_client_id", "invoices", ["client_id"], unique=False)
    op.create_index(
        "ix_invoices_legal_entity_doc_date", "invoices", ["legal_entity_id", "doc_date"], unique=False
    )
    op.create_index(op.f("ix_invoices_tenant_id"), "invoices", ["tenant_id"], unique=False)
    op.create_index(
        "uq_invoices_number",
        "invoices",
        ["tenant_id", "series_id", "fiscal_year", "number"],
        unique=True,
        postgresql_where=sa.text("number IS NOT NULL"),
    )
    op.create_table(
        "debit_notes",
        sa.Column("original_invoice_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("supplier_gst_registration_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("series_id", sa.Uuid(), nullable=True),
        sa.Column("fiscal_year", sa.CHAR(length=5), nullable=True),
        sa.Column("number", sa.String(length=16), nullable=True),
        sa.Column("doc_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column(
            "document_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("is_imported", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("cancel_basis", sa.String(length=20), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "cancel_basis IS NULL OR cancel_basis IN ('irn_cancelled')",
            name=op.f("ck_debit_notes_cancel_basis"),
        ),
        sa.CheckConstraint(
            "number IS NULL OR number ~ '^[A-Za-z0-9/-]{1,16}$'", name=op.f("ck_debit_notes_number_format")
        ),
        sa.CheckConstraint(
            "status <> 'cancelled' OR cancel_basis IS NOT NULL",
            name=op.f("ck_debit_notes_cancel_basis_required"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'issued', 'discarded', 'cancelled')",
            name=op.f("ck_debit_notes_status"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('issued', 'cancelled') OR (number IS NOT NULL AND doc_date IS NOT NULL)",
            name=op.f("ck_debit_notes_issued_has_number"),
        ),
        sa.ForeignKeyConstraint(
            ["currency"],
            ["currencies.code"],
            name=op.f("fk_debit_notes_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_debit_notes_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_debit_notes_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "original_invoice_id"],
            ["invoices.tenant_id", "invoices.id"],
            name="fk_debit_notes_original_invoice_id_invoices",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "series_id"],
            ["document_series.tenant_id", "document_series.id"],
            name="fk_debit_notes_series_id_document_series",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "supplier_gst_registration_id"],
            ["gst_registrations.tenant_id", "gst_registrations.id"],
            name="fk_debit_notes_supplier_gst_registration_id_gst_registrations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_debit_notes_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_debit_notes")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_debit_notes_tenant_id_id")),
    )
    op.create_index("ix_debit_notes_client_id", "debit_notes", ["client_id"], unique=False)
    op.create_index(
        "ix_debit_notes_legal_entity_doc_date", "debit_notes", ["legal_entity_id", "doc_date"], unique=False
    )
    op.create_index(
        "ix_debit_notes_original_invoice_id", "debit_notes", ["original_invoice_id"], unique=False
    )
    op.create_index(op.f("ix_debit_notes_tenant_id"), "debit_notes", ["tenant_id"], unique=False)
    op.create_index(
        "uq_debit_notes_number",
        "debit_notes",
        ["tenant_id", "series_id", "fiscal_year", "number"],
        unique=True,
        postgresql_where=sa.text("number IS NOT NULL"),
    )
    for table in TABLES:
        enable_rls(table)

    op.execute(
        "ALTER TABLE rate_segments ADD CONSTRAINT ex_rate_segments_no_overlap "
        "EXCLUDE USING gist (placement_id WITH =, daterange(effective_from, effective_to, '[]') WITH &&)"
    )
    op.execute(PLACEMENT_PARTIES_GUARD)
    op.execute(
        "CREATE TRIGGER tos_placement_parties_guard BEFORE UPDATE ON placements "
        "FOR EACH ROW EXECUTE FUNCTION tos_placement_parties_guard()"
    )
    enable_immutability("rate_segments", status_column=None, allowed_columns=("effective_to",))
    for table in DOCUMENTS:
        enable_immutability(
            table,
            status_column="status",
            mutable_states=("draft", "pending_approval"),
            allowed_columns=(),
            allowed_transitions=(("issued", "cancelled"),),
            transition_columns=("cancel_basis", "cancelled_at"),
        )
        enable_period_guard(table, date_column="doc_date")
        enable_org_rls(table, client_col="client_id")

    enable_org_rls("contracts", client_col="client_id", vendor_col="vendor_id")
    enable_org_rls("purchase_orders", client_col="client_id")
    enable_org_rls("placements", client_col="client_id", vendor_col="vendor_id", worker_col="candidate_id")
    enable_org_rls(
        "rate_segments",
        client_col="client_id",
        vendor_col="vendor_id",
        worker_col="candidate_id",
        inherit_from=InheritRef("placements", "placement_id", ("client_id", "vendor_id", "candidate_id")),
    )


def downgrade() -> None:
    for table in DOCUMENTS:
        disable_period_guard(table)
        disable_immutability(table)
    disable_immutability("rate_segments")
    op.execute("DROP TRIGGER IF EXISTS tos_placement_parties_guard ON placements")
    op.execute("DROP FUNCTION IF EXISTS tos_placement_parties_guard()")
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
