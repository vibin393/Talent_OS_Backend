# ruff: noqa: E501
"""Epic 30 217_02: ``tds_sections`` (+ platform seed), ``tds_deductions``, ``tds_deduction_reversals``,
``write_off_requests``, ``write_offs``, ``write_off_reversals``, ``disputes`` (FR-154, FR-157, FR-158).

Guards: TDS and write-off facts are immutable (TDS: certificate fields only) and period-guarded on their date
column. ``tds_sections`` is hybrid (platform rows ``tenant_id`` NULL + tenant copies, mixed RLS).

Revision ID: 20261005_217_02_rcv_tds_wo
Revises: 20261005_217_01_rcv_payments
Create Date: 2026-10-05
"""

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    disable_immutability,
    disable_org_rls,
    disable_period_guard,
    disable_rls,
    enable_immutability,
    enable_mixed_rls,
    enable_org_rls,
    enable_period_guard,
    enable_rls,
)

revision: str = "20261005_217_02_rcv_tds_wo"
down_revision: str | tuple[str, ...] | None = "20261005_217_01_rcv_payments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CERT_COLUMNS = ("certificate_status", "certificate_ref", "certificate_received_at")
ONE_TARGET = "(invoice_id IS NOT NULL) <> (debit_note_id IS NOT NULL)"
SEED = (
    ("income_tax", "194C", "194C Contractors (individual / HUF) 1% (legacy section)", "1.0000", "Form 16A"),
    ("income_tax", "194C-2", "194C Contractors (others) 2% (legacy section)", "2.0000", "Form 16A"),
    ("income_tax", "194J", "194J Fees for technical services 2% (legacy section)", "2.0000", "Form 16A"),
    ("income_tax", "194J-10", "194J Fees for professional services 10% (legacy section)", "10.0000", "Form 16A"),
    ("gst_tds", "GST-TDS", "GST TDS under section 51 (2%)", "2.0000", "GSTR-7A"),
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


def money(name: str, nullable: bool = False, default: str | None = None) -> sa.Column:
    return sa.Column(name, sa.Numeric(16, 2), nullable=nullable, server_default=sa.text(default) if default else None)


def targets(table: str) -> list[Any]:
    return [
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("debit_note_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(ONE_TARGET, name=f"ck_{table}_one_target"),
        tfk("invoice_id", "invoices", f"fk_{table}_invoice_id_invoices"),
        tfk("debit_note_id", "debit_notes", f"fk_{table}_debit_note_id_debit_notes"),
    ]


def upgrade() -> None:
    op.create_table(
        "tds_sections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("default_rate_pct", sa.Numeric(7, 4), nullable=False),
        sa.Column("certificate_name", sa.String(40), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tds_sections"),
        sa.CheckConstraint("kind IN ('income_tax', 'gst_tds')", name="ck_tds_sections_kind"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_tds_sections_tenant_id_tenants", ondelete="RESTRICT"),
    )
    op.create_index("ix_tds_sections_tenant", "tds_sections", ["tenant_id"])
    seed = sa.table(
        "tds_sections",
        sa.column("id", sa.Uuid()),
        sa.column("kind", sa.String()),
        sa.column("code", sa.String()),
        sa.column("label", sa.String()),
        sa.column("default_rate_pct", sa.Numeric()),
        sa.column("certificate_name", sa.String()),
    )
    op.bulk_insert(
        seed,
        [
            {"id": uuid.uuid5(uuid.NAMESPACE_URL, f"talent-os/tds/{code}"), "kind": kind, "code": code, "label": label, "default_rate_pct": rate, "certificate_name": cert}
            for kind, code, label, rate, cert in SEED
        ],
    )

    op.create_table(
        "tds_deductions",
        *base("tds_deductions"),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        *targets("tds_deductions"),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=True),
        sa.Column("section_code", sa.String(20), nullable=True),
        sa.Column("rate_pct", sa.Numeric(7, 4), nullable=False),
        money("base_amount"),
        money("expected_amount"),
        money("amount"),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        money("inr_amount"),
        sa.Column("variance_flag", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("deduction_date", sa.Date(), nullable=False),
        sa.Column("quarter", sa.String(10), nullable=False),
        sa.Column("certificate_status", sa.String(10), server_default=sa.text("'expected'"), nullable=False),
        sa.Column("certificate_ref", sa.String(100), nullable=True),
        sa.Column("certificate_received_at", sa.Date(), nullable=True),
        sa.Column("prior_period", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tds_deductions_tenant_id_id"),
        sa.CheckConstraint("kind IN ('income_tax', 'gst_tds')", name="ck_tds_deductions_kind"),
        sa.CheckConstraint("certificate_status IN ('expected', 'received', 'mismatch')", name="ck_tds_deductions_certificate_status"),
        sa.CheckConstraint("amount > 0", name="ck_tds_deductions_amount_positive"),
        tfk("legal_entity_id", "legal_entities", "fk_tds_deductions_legal_entity_id_legal_entities"),
        tfk("payment_id", "payments", "fk_tds_deductions_payment_id_payments"),
    )
    op.create_index("ix_tds_deductions_tenant_id", "tds_deductions", ["tenant_id"])
    op.create_index("ix_tds_deductions_invoice", "tds_deductions", ["tenant_id", "invoice_id"])
    op.create_index("ix_tds_deductions_debit_note", "tds_deductions", ["tenant_id", "debit_note_id"])
    op.create_index("ix_tds_deductions_client_quarter", "tds_deductions", ["tenant_id", "client_id", "quarter"])

    op.create_table(
        "tds_deduction_reversals",
        *base("tds_deduction_reversals"),
        sa.Column("tds_deduction_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("reversal_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("prior_period", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.UniqueConstraint("tenant_id", "tds_deduction_id", name="uq_tds_deduction_reversals_deduction"),
        tfk("tds_deduction_id", "tds_deductions", "fk_tds_deduction_reversals_deduction"),
        tfk("legal_entity_id", "legal_entities", "fk_tds_deduction_reversals_le"),
    )
    op.create_index("ix_tds_deduction_reversals_tenant_id", "tds_deduction_reversals", ["tenant_id"])

    op.create_table(
        "write_off_requests",
        *base("write_off_requests"),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        *targets("write_off_requests"),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        money("requested_amount"),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending_approval'"), nullable=False),
        sa.Column("status_reason", sa.String(300), nullable=True),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("write_off_id", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_write_off_requests_tenant_id_id"),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'approved', 'rejected', 'executed', 'lapsed')", name="ck_write_off_requests_status"
        ),
        sa.CheckConstraint("requested_amount > 0", name="ck_write_off_requests_amount_positive"),
        tfk("legal_entity_id", "legal_entities", "fk_write_off_requests_le_legal_entities"),
    )
    op.create_index("ix_write_off_requests_tenant_id", "write_off_requests", ["tenant_id"])

    op.create_table(
        "write_offs",
        *base("write_offs"),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        *targets("write_offs"),
        money("amount"),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("write_off_date", sa.Date(), nullable=False),
        money("inr_amount"),
        money("residual_fx", default="0"),
        sa.Column("prior_period", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_write_offs_tenant_id_id"),
        sa.CheckConstraint("amount > 0", name="ck_write_offs_amount_positive"),
        tfk("legal_entity_id", "legal_entities", "fk_write_offs_legal_entity_id_legal_entities"),
        tfk("request_id", "write_off_requests", "fk_write_offs_request_id_write_off_requests"),
    )
    op.create_index("ix_write_offs_tenant_id", "write_offs", ["tenant_id"])
    op.create_index("ix_write_offs_invoice", "write_offs", ["tenant_id", "invoice_id"])
    op.create_index("ix_write_offs_debit_note", "write_offs", ["tenant_id", "debit_note_id"])

    op.create_table(
        "write_off_reversals",
        *base("write_off_reversals"),
        sa.Column("write_off_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("reversal_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("prior_period", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.UniqueConstraint("tenant_id", "write_off_id", name="uq_write_off_reversals_write_off"),
        tfk("write_off_id", "write_offs", "fk_write_off_reversals_write_off_id_write_offs"),
        tfk("legal_entity_id", "legal_entities", "fk_write_off_reversals_le"),
    )
    op.create_index("ix_write_off_reversals_tenant_id", "write_off_reversals", ["tenant_id"])

    op.create_table(
        "disputes",
        *base("disputes"),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        *targets("disputes"),
        sa.Column("line_ids", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        money("amount"),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(24), server_default=sa.text("'open'"), nullable=False),
        sa.Column("resolution_note", sa.String(1000), nullable=True),
        sa.Column("linked_credit_note_id", sa.Uuid(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved_credit_note', 'resolved_reissue', 'rejected')", name="ck_disputes_status"
        ),
        sa.CheckConstraint("amount > 0", name="ck_disputes_amount_positive"),
        tfk("legal_entity_id", "legal_entities", "fk_disputes_legal_entity_id_legal_entities"),
    )
    op.create_index("ix_disputes_tenant_id", "disputes", ["tenant_id"])
    op.create_index("ix_disputes_client_status", "disputes", ["tenant_id", "client_id", "status"])

    enable_mixed_rls("tds_sections")
    for table in ("tds_deductions", "tds_deduction_reversals", "write_off_requests", "write_offs", "write_off_reversals", "disputes"):
        enable_rls(table)
    for table in ("tds_deductions", "write_offs", "disputes"):
        enable_org_rls(table, client_col="client_id")
    enable_immutability("tds_deductions", status_column=None, allowed_columns=CERT_COLUMNS)
    enable_period_guard("tds_deductions", date_column="deduction_date", allowed_columns=CERT_COLUMNS)
    for table, date_column in (("tds_deduction_reversals", "reversal_date"), ("write_offs", "write_off_date"), ("write_off_reversals", "reversal_date")):
        enable_immutability(table, status_column=None)
        enable_period_guard(table, date_column=date_column)


def downgrade() -> None:
    for table in ("tds_deductions", "tds_deduction_reversals", "write_offs", "write_off_reversals"):
        disable_period_guard(table)
        disable_immutability(table)
    for table in ("tds_deductions", "write_offs", "disputes"):
        disable_org_rls(table)
    for table in ("disputes", "write_off_reversals", "write_offs", "write_off_requests", "tds_deduction_reversals", "tds_deductions", "tds_sections"):
        disable_rls(table)
        op.drop_table(table)
