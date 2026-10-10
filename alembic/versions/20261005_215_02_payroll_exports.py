# ruff: noqa: E501
"""Epic 28 (Payroll Export) 215_02: ``payroll_export_versions``, ``payroll_export_rows``,
``payroll_export_items`` (FR-141, AD-21 / AD-23) and ``payroll_export_mappings`` (A6).

Versions are mutable only while ``building|generated|superseded`` (previews and a final being written in its
transaction); once ``finalized`` only the provider columns change and ``finalized -> processed`` is allowed.
Rows / items follow their parent's status (INSERT / UPDATE / DELETE refused once the parent is final).
Internal-only tables (org RLS without org columns).

Revision ID: 20261005_215_02_payroll_exports
Revises: 20261005_215_01_payroll_periods
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    ParentRef,
    disable_immutability,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261005_215_02_payroll_exports"
down_revision: str | tuple[str, ...] | None = "20261005_215_01_payroll_periods"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

V = "payroll_export_versions"
R = "payroll_export_rows"
I = "payroll_export_items"  # noqa: E741
M = "payroll_export_mappings"
_MUTABLE = ("building", "generated", "superseded")
_PROVIDER = ("provider_status", "provider_reference")


def _tenant(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
    )


def _tfk(table: str, column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    del table
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _num(name: str, p: int = 16, s: int = 2) -> sa.Column:
    return sa.Column(name, sa.Numeric(p, s), server_default=sa.text("0"), nullable=False)


def upgrade() -> None:
    op.create_table(
        V,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("pay_period_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(14), nullable=False),
        sa.Column("supplementary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'building'"), nullable=False),
        sa.Column("based_on_preview_id", sa.Uuid(), nullable=True),
        sa.Column("exclude_exceptions", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("generated_by", sa.Uuid(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_by", sa.Uuid(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_hash", sa.CHAR(64), nullable=True),
        sa.Column("content_hash", sa.CHAR(64), nullable=True),
        sa.Column("exceptions", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("totals", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("row_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("item_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("provider_status", sa.String(10), server_default=sa.text("'not_sent'"), nullable=False),
        sa.Column("provider_reference", sa.String(200), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_by", sa.Uuid(), nullable=True),
        *_ts(),
        _tenant(V),
        sa.PrimaryKeyConstraint("id", name=f"pk_{V}"),
        _tfk(V, "pay_period_id", "pay_periods", "fk_payroll_export_versions_pay_period_id"),
        _tfk(V, "legal_entity_id", "legal_entities", "fk_payroll_export_versions_legal_entity_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_payroll_export_versions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "pay_period_id", "seq", name="uq_payroll_export_versions_tenant_id_pay_period_id_seq"
        ),
        sa.CheckConstraint("kind IN ('preview', 'final', 'supplementary')", name=op.f(f"ck_{V}_kind")),
        sa.CheckConstraint(
            "status IN ('building', 'generated', 'finalized', 'processed', 'superseded')",
            name=op.f(f"ck_{V}_status"),
        ),
        sa.CheckConstraint(
            "provider_status IN ('not_sent', 'sent', 'processed', 'failed')", name=op.f(f"ck_{V}_provider_status")
        ),
    )
    op.create_index(f"ix_{V}_tenant_id", V, ["tenant_id"])
    op.create_index(f"ix_{V}_pay_period_id", V, ["pay_period_id"])
    op.create_index(
        "uq_payroll_export_versions_one_final",
        V,
        ["pay_period_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'final'"),
    )

    op.create_table(
        R,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("worker_human_id", sa.String(20), nullable=True),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("placement_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("worker_type", sa.String(20), nullable=True),
        sa.Column("pan_last4", sa.String(4), nullable=True),
        sa.Column("bank_last4", sa.String(4), nullable=True),
        sa.Column("ifsc", sa.String(11), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        *[
            _num(f"hours_{c}", 8, 2)
            for c in ("regular", "ot", "double_time", "holiday", "leave_paid", "leave_unpaid")
        ],
        _num("days_leave_paid", 6, 2),
        _num("days_leave_unpaid", 6, 2),
        *[_num(f"earnings_{c}") for c in ("regular", "ot", "double_time", "holiday", "leave_paid", "base")],
        sa.Column("pay_rates", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        _num("reimbursements_total"),
        _num("adjustments_total"),
        _num("rate_adjustments_total"),
        _num("gross_earnings_inputs"),
        _tenant(R),
        sa.PrimaryKeyConstraint("id", name=f"pk_{R}"),
        _tfk(R, "version_id", V, "fk_payroll_export_rows_version_id", "CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_payroll_export_rows_tenant_id_id"),
        sa.UniqueConstraint(
            "version_id", "candidate_id", "currency", name="uq_payroll_export_rows_version_id_candidate_id_currency"
        ),
    )
    op.create_index(f"ix_{R}_tenant_id", R, ["tenant_id"])
    op.create_index(f"ix_{R}_candidate_id", R, ["candidate_id"])

    op.create_table(
        I,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("row_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("rate_segment_id", sa.Uuid(), nullable=True),
        sa.Column("bucket", sa.String(16), nullable=False),
        sa.Column("category", sa.String(40), nullable=True),
        sa.Column("item_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Numeric(8, 2), nullable=False),
        sa.Column("rate", sa.Numeric(14, 4), nullable=True),
        sa.Column("multiplier", sa.Numeric(14, 4), nullable=True),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        _tenant(I),
        sa.PrimaryKeyConstraint("id", name=f"pk_{I}"),
        _tfk(I, "version_id", V, "fk_payroll_export_items_version_id", "CASCADE"),
        _tfk(I, "row_id", R, "fk_payroll_export_items_row_id", "CASCADE"),
    )
    op.create_index(f"ix_{I}_tenant_id", I, ["tenant_id"])
    op.create_index(f"ix_{I}_version_id", I, ["version_id"])
    op.create_index(f"ix_{I}_row_id", I, ["row_id"])
    op.create_index(f"ix_{I}_source", I, ["source_kind", "source_id"])

    op.create_table(
        M,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("format", sa.String(4), server_default=sa.text("'csv'"), nullable=False),
        sa.Column("columns", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_ts(),
        _tenant(M),
        sa.PrimaryKeyConstraint("id", name=f"pk_{M}"),
        _tfk(M, "legal_entity_id", "legal_entities", "fk_payroll_export_mappings_legal_entity_id"),
        sa.CheckConstraint("format IN ('csv', 'xlsx')", name=op.f(f"ck_{M}_format")),
    )
    op.create_index(f"ix_{M}_tenant_id", M, ["tenant_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_payroll_export_mappings_default ON payroll_export_mappings "
        "(tenant_id, COALESCE(legal_entity_id, '00000000-0000-0000-0000-000000000000'::uuid)) WHERE is_default"
    )

    for table in (V, R, I, M):
        enable_rls(table)
        enable_org_rls(table)
    enable_immutability(
        V,
        status_column="status",
        mutable_states=_MUTABLE,
        allowed_columns=_PROVIDER,
        allowed_transitions=(("finalized", "processed"),),
        transition_columns=("processed_at", "processed_by"),
    )
    for child in (R, I):
        enable_immutability(child, status_column=None, parent=ParentRef(V, "version_id", "status", _MUTABLE))


def downgrade() -> None:
    for table in (M, I, R, V):
        if table != M:
            disable_immutability(table)
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
