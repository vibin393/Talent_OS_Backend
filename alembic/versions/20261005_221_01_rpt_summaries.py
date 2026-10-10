"""Epic 34 (Dashboards & Reporting) 221_01: rebuildable summary tables and reconciliation issues (AD-33).

``reporting_finance_monthly`` (LE, month, client), ``reporting_ops_monthly`` (LE, month) and
``reporting_reconciliation_issues``. Derived data: tenant RLS, not audited. No FKs into other epics' tables
(legal entities / clients / invoices are carried as ids only), so this branch needs no ``depends_on``.

Revision ID: 20261005_221_01_rpt_summaries
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261005_221_01_rpt_summaries"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_reporting",)
depends_on: str | Sequence[str] | None = None

_MONEY = sa.Numeric(16, 2)


def _base() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
    ]


def _fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_table(
        "reporting_finance_monthly",
        *_base(),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("revenue_inr", _MONEY, server_default=sa.text("0"), nullable=False),
        sa.Column("invoiced_with_tax_inr", _MONEY, server_default=sa.text("0"), nullable=False),
        sa.Column("cost_inr", _MONEY, server_default=sa.text("0"), nullable=False),
        sa.Column("credit_inr", _MONEY, server_default=sa.text("0"), nullable=False),
        sa.Column("debit_inr", _MONEY, server_default=sa.text("0"), nullable=False),
        sa.Column("invoice_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("credit_note_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_reporting_finance_monthly"),
        _fk("reporting_finance_monthly"),
        sa.UniqueConstraint(
            "tenant_id", "legal_entity_id", "month", "client_id", name="uq_rpt_fin_monthly_key"
        ),
    )
    op.create_index(
        "ix_rpt_fin_monthly_le_month", "reporting_finance_monthly", ["tenant_id", "legal_entity_id", "month"]
    )
    enable_rls("reporting_finance_monthly")

    op.create_table(
        "reporting_ops_monthly",
        *_base(),
        sa.Column("workers_on_billing", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("placements_started", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("placements_ended", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("early_terminations", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("positions_closed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("positions_filled", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_reporting_ops_monthly"),
        _fk("reporting_ops_monthly"),
        sa.UniqueConstraint("tenant_id", "legal_entity_id", "month", name="uq_rpt_ops_monthly_key"),
    )
    enable_rls("reporting_ops_monthly")

    op.create_table(
        "reporting_reconciliation_issues",
        *_base(),
        sa.Column("figure", sa.String(40), nullable=False),
        sa.Column("summary_value", sa.String(40), nullable=True),
        sa.Column("live_value", sa.String(40), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_reporting_reconciliation_issues"),
        _fk("reporting_reconciliation_issues"),
    )
    op.create_index(
        "ix_rpt_recon_open",
        "reporting_reconciliation_issues",
        ["tenant_id", "legal_entity_id", "month", "resolved_at"],
    )
    enable_rls("reporting_reconciliation_issues")


def downgrade() -> None:
    for table in ("reporting_reconciliation_issues", "reporting_ops_monthly", "reporting_finance_monthly"):
        disable_rls(table)
        op.drop_table(table)
