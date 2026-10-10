"""Epic 27 (Expenses) 214_02: ``expense_claims``, ``expense_lines``, ``expense_line_decisions`` (FR-138/139,
AD-21, AD-23, AD-26).

* Org RLS on all three (client / vendor / worker columns); lines and decisions inherit them from the claim.
* ``expense_lines`` are immutable unless the claim is ``draft`` / ``rejected`` -- the decision columns
  (``line_status``, ``decision_comment``, ``bill_amount``) excepted -- and while any non-released
  posting of the
  line exists in either ledger (``tos_source_lock``, source kind ``expense_line``).
* ``expense_claims``: free while ``draft`` / ``submitted`` / ``in_approval`` / ``rejected``; once
approved only
  the system transitions to ``reimbursed`` / ``stated`` and the reopen back to ``draft`` are
  allowed (with their
  bookkeeping columns); ``reimbursed`` / ``stated`` are final.

Revision ID: 20261005_214_02_expense_claims
Revises: 20261005_214_01_expense_policies
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    InheritRef,
    ParentRef,
    disable_immutability,
    disable_org_rls,
    disable_rls,
    disable_source_lock,
    enable_immutability,
    enable_org_rls,
    enable_rls,
    enable_source_lock,
)

revision: str = "20261005_214_02_expense_claims"
down_revision: str | tuple[str, ...] | None = "20261005_214_01_expense_policies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG = ("client_id", "vendor_id", "candidate_id")
_CATEGORIES = "category IN ('travel', 'accommodation', 'meals', 'mileage', 'equipment', 'other')"
_STATUSES = (
    "status IN ('draft', 'submitted', 'in_approval', 'approved', 'partially_approved', 'rejected', "
    "'reimbursed', 'stated')"
)
CLAIM_TRANSITION_COLUMNS = (
    "reimbursed_at",
    "payroll_reference",
    "export_version_id",
    "stated_at",
    "vendor_statement_id",
    "reopen_reason",
    "total_approved",
    "approved_at",
    "submitted_at",
    "submitted_by_kind",
    "submitted_by_id",
    "approval_request_id",
    "lock_version",
)


def _tfk(
    table: str, column: str, target: str, ondelete: str = "RESTRICT", name: str | None = None
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=name or f"fk_{table}_{column}_{target}",
        ondelete=ondelete,
    )


def _tenant(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
    )


def _org_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "expense_claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_by_kind", sa.String(20), nullable=True),
        sa.Column("submitted_by_id", sa.Uuid(), nullable=True),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("total_claimed", sa.Numeric(16, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("total_approved", sa.Numeric(16, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("is_proxy", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("proxy_reason", sa.Text(), nullable=True),
        sa.Column("rejection_comment", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reimbursed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payroll_reference", sa.String(200), nullable=True),
        sa.Column("export_version_id", sa.Uuid(), nullable=True),
        sa.Column("stated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vendor_statement_id", sa.Uuid(), nullable=True),
        sa.Column("reopen_reason", sa.Text(), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(_STATUSES, name="ck_expense_claims_status"),
        sa.CheckConstraint("total_claimed >= 0 AND total_approved >= 0", name="ck_expense_claims_totals"),
        _tenant("expense_claims"),
        _tfk("expense_claims", "placement_id", "placements"),
        _tfk("expense_claims", "candidate_id", "candidates"),
        _tfk("expense_claims", "client_id", "clients"),
        _tfk("expense_claims", "legal_entity_id", "legal_entities"),
        sa.ForeignKeyConstraint(
            ["approval_request_id"],
            ["approval_requests.id"],
            name="fk_expense_claims_approval_request_id_approval_requests",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expense_claims"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_expense_claims_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_expense_claims_tenant_id_human_id"),
    )
    op.create_index("ix_expense_claims_tenant_id", "expense_claims", ["tenant_id"])
    op.create_index("ix_expense_claims_placement_id", "expense_claims", ["placement_id"])
    op.create_index("ix_expense_claims_candidate_id", "expense_claims", ["candidate_id"])
    op.create_index("ix_expense_claims_client_id", "expense_claims", ["client_id"])
    op.create_index("ix_expense_claims_status", "expense_claims", ["status"])

    op.create_table(
        "expense_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("km", sa.Numeric(10, 2), nullable=True),
        sa.Column("fx_rate", sa.Numeric(18, 6), nullable=True),
        sa.Column("fx_source", sa.String(40), nullable=True),
        sa.Column("fx_rate_date", sa.Date(), nullable=True),
        sa.Column("amount_claim_ccy", sa.Numeric(16, 2), nullable=False),
        sa.Column("receipt_file_id", sa.Uuid(), nullable=True),
        sa.Column("receipt_sha256", sa.String(64), nullable=True),
        sa.Column("billable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reimbursable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("policy_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "flags", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("line_status", sa.String(10), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("rebill_method", sa.String(20), nullable=True),
        sa.Column("markup_pct", sa.Numeric(7, 4), nullable=True),
        sa.Column("rebill_tax_mode", sa.String(20), nullable=True),
        sa.Column("bill_amount", sa.Numeric(16, 2), nullable=True),
        *_org_columns(),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(_CATEGORIES, name="ck_expense_lines_category"),
        sa.CheckConstraint(
            "line_status IN ('pending', 'approved', 'rejected')", name="ck_expense_lines_line_status"
        ),
        sa.CheckConstraint(
            "rebill_method IS NULL OR rebill_method IN ('at_cost', 'markup', 'not_billable')",
            name="ck_expense_lines_rebill_method",
        ),
        sa.CheckConstraint(
            "rebill_tax_mode IS NULL OR rebill_tax_mode IN ('taxable', 'pure_agent')",
            name="ck_expense_lines_rebill_tax_mode",
        ),
        sa.CheckConstraint(
            "amount >= 0 AND amount_claim_ccy >= 0 AND (km IS NULL OR km >= 0) "
            "AND (bill_amount IS NULL OR bill_amount >= 0)",
            name="ck_expense_lines_non_negative",
        ),
        _tenant("expense_lines"),
        _tfk("expense_lines", "claim_id", "expense_claims", "CASCADE"),
        sa.ForeignKeyConstraint(
            ["receipt_file_id"],
            ["stored_files.id"],
            name="fk_expense_lines_receipt_file_id_stored_files",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expense_lines"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_expense_lines_tenant_id_id"),
    )
    op.create_index("ix_expense_lines_tenant_id", "expense_lines", ["tenant_id"])
    op.create_index("ix_expense_lines_claim_id", "expense_lines", ["claim_id"])
    op.create_index("ix_expense_lines_expense_date", "expense_lines", ["expense_date"])
    op.create_index("ix_expense_lines_receipt_sha256", "expense_lines", ["receipt_sha256"])

    op.create_table(
        "expense_line_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("step", sa.String(100), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *_org_columns(),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'passed')", name="ck_expense_line_decisions_decision"
        ),
        _tenant("expense_line_decisions"),
        _tfk(
            "expense_line_decisions",
            "line_id",
            "expense_lines",
            "CASCADE",
            name="fk_expense_line_decisions_line_id_expense_lines",
        ),
        _tfk(
            "expense_line_decisions",
            "claim_id",
            "expense_claims",
            "CASCADE",
            name="fk_expense_line_decisions_claim_id_expense_claims",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expense_line_decisions"),
    )
    op.create_index("ix_expense_line_decisions_tenant_id", "expense_line_decisions", ["tenant_id"])
    op.create_index("ix_expense_line_decisions_line_id", "expense_line_decisions", ["line_id"])
    op.create_index("ix_expense_line_decisions_claim_id", "expense_line_decisions", ["claim_id"])

    for table in ("expense_claims", "expense_lines", "expense_line_decisions"):
        enable_rls(table)
    enable_org_rls(
        "expense_claims", client_col="client_id", vendor_col="vendor_id", worker_col="candidate_id"
    )
    for table in ("expense_lines", "expense_line_decisions"):
        enable_org_rls(
            table,
            client_col="client_id",
            vendor_col="vendor_id",
            worker_col="candidate_id",
            inherit_from=InheritRef("expense_claims", "claim_id", ORG),
        )
    enable_immutability(
        "expense_claims",
        status_column="status",
        mutable_states=("draft", "submitted", "in_approval", "rejected"),
        allowed_transitions=(
            ("approved", "reimbursed"),
            ("partially_approved", "reimbursed"),
            ("approved", "stated"),
            ("partially_approved", "stated"),
            ("approved", "draft"),
            ("partially_approved", "draft"),
        ),
        transition_columns=CLAIM_TRANSITION_COLUMNS,
    )
    enable_immutability(
        "expense_lines",
        status_column=None,
        parent=ParentRef("expense_claims", "claim_id", "status", ("draft", "rejected")),
        allowed_columns=("line_status", "decision_comment", "bill_amount"),
    )
    enable_source_lock("expense_lines", source_kind="expense_line")


def downgrade() -> None:
    disable_source_lock("expense_lines")
    disable_immutability("expense_lines")
    disable_immutability("expense_claims")
    for table in ("expense_line_decisions", "expense_lines", "expense_claims"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
