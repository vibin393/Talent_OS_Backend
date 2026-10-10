"""Expense tables (Epic 27, FR-137..FR-139). Branch ``p2_expenses``, migrations ``20261005_214_01..02``.

* ``expense_settings`` (one row per tenant, lazy).
* ``expense_policies`` (versioned per scope: tenant / client; a change is a new version effective from a date)
  and ``expense_policy_categories`` (per-category limits, receipt threshold, mileage rate, re-bill terms).
* ``expense_claims`` / ``expense_lines`` / ``expense_line_decisions``: org columns copied from the placement
  drive org RLS (client, vendor, worker). ``status`` is written only by ``expenses.claims.transition_claim``
  (AD-4). Lines are immutable unless the claim is ``draft`` / ``rejected`` (decision columns
  excepted) and while
  any non-released posting of the line exists in either ledger (``tos_immutable`` + ``tos_source_lock``).

Money is NUMERIC(16,2), FX NUMERIC(18,6), mileage rates NUMERIC(14,4), markup NUMERIC(7,4) (AD-20).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

EXPENSE_CATEGORIES = ("travel", "accommodation", "meals", "mileage", "equipment", "other")
POLICY_SCOPES = ("tenant", "client")
REBILL_METHODS = ("at_cost", "markup", "not_billable")
REBILL_TAX_MODES = ("taxable", "pure_agent")
CLAIM_STATUSES = (
    "draft",
    "submitted",
    "in_approval",
    "approved",
    "partially_approved",
    "rejected",
    "reimbursed",
    "stated",
)
LINE_STATUSES = ("pending", "approved", "rejected")
LINE_FLAGS = (
    "over_daily_limit",
    "over_claim_limit",
    "receipt_missing",
    "possible_duplicate_receipt",
    "category_disabled",
)
DEFAULT_RECEIPT_TYPES = ("image/jpeg", "image/png", "image/heic", "application/pdf")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class ExpenseSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "expense_settings"
    __audit_entity_id__ = "tenant_id"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    ai_receipt_read_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    duplicate_receipt_window_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("90")
    )
    max_receipt_mb: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    allowed_receipt_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{image/jpeg,image/png,image/heic,application/pdf}'::text[]"),
    )


class ExpensePolicy(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per version of a (scope, client) policy; rows are never edited except ``superseded_at``."""

    __tablename__ = "expense_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("scope", POLICY_SCOPES), name="scope"),
        CheckConstraint(
            "(scope = 'tenant' AND client_id IS NULL) OR (scope = 'client' AND client_id IS NOT NULL)",
            name="scope_client",
        ),
        CheckConstraint("version >= 1", name="version"),
        _tenant_fk("client_id", "clients", "fk_expense_policies_client_id_clients"),
        Index(
            "uq_expense_policies_tenant_version",
            "tenant_id",
            "version",
            unique=True,
            postgresql_where=text("scope = 'tenant'"),
        ),
        Index(
            "uq_expense_policies_client_version",
            "tenant_id",
            "client_id",
            "version",
            unique=True,
            postgresql_where=text("scope = 'client'"),
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ExpensePolicyCategory(UUIDPk, TenantMixin, Base):
    __tablename__ = "expense_policy_categories"
    __table_args__ = (
        UniqueConstraint("policy_id", "category"),
        CheckConstraint(_in("category", EXPENSE_CATEGORIES), name="category"),
        CheckConstraint(_in("rebill_method", REBILL_METHODS), name="rebill_method"),
        CheckConstraint(_in("rebill_tax_mode", REBILL_TAX_MODES), name="rebill_tax_mode"),
        CheckConstraint(
            "(daily_limit IS NULL OR daily_limit >= 0) AND (claim_limit IS NULL OR claim_limit >= 0) "
            "AND (receipt_required_above IS NULL OR receipt_required_above >= 0) "
            "AND (mileage_rate_per_km IS NULL OR mileage_rate_per_km >= 0) "
            "AND (markup_pct IS NULL OR markup_pct >= 0)",
            name="non_negative",
        ),
        _tenant_fk("policy_id", "expense_policies", "fk_expense_policy_categories_policy_id", "CASCADE"),
        Index("ix_expense_policy_categories_policy_id", "policy_id"),
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    daily_limit: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    claim_limit: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    limit_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'INR'"))
    receipt_required_above: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    mileage_rate_per_km: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    default_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    rebill_method: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'at_cost'"))
    markup_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    rebill_tax_mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'taxable'"))


class ExpenseClaim(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "expense_claims"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", CLAIM_STATUSES), name="status"),
        CheckConstraint("total_claimed >= 0 AND total_approved >= 0", name="totals"),
        _tenant_fk("placement_id", "placements", "fk_expense_claims_placement_id_placements"),
        _tenant_fk("candidate_id", "candidates", "fk_expense_claims_candidate_id_candidates"),
        _tenant_fk("client_id", "clients", "fk_expense_claims_client_id_clients"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_expense_claims_legal_entity_id_legal_entities"),
        Index("ix_expense_claims_placement_id", "placement_id"),
        Index("ix_expense_claims_candidate_id", "candidate_id"),
        Index("ix_expense_claims_client_id", "client_id"),
        Index("ix_expense_claims_status", "status"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_by_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    submitted_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("approval_requests.id", ondelete="SET NULL"), nullable=True
    )
    total_claimed: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"))
    total_approved: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"))
    is_proxy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    proxy_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reimbursed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payroll_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    export_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    stated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vendor_statement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reopen_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class _InheritedOrg:
    """Org columns copied from the parent claim by ``tos_inherit_org``."""

    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ExpenseLine(UUIDPk, TenantMixin, Timestamps, _InheritedOrg, Base):
    __tablename__ = "expense_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("category", EXPENSE_CATEGORIES), name="category"),
        CheckConstraint(_in("line_status", LINE_STATUSES), name="line_status"),
        CheckConstraint(
            f"rebill_method IS NULL OR {_in('rebill_method', REBILL_METHODS)}", name="rebill_method"
        ),
        CheckConstraint(
            f"rebill_tax_mode IS NULL OR {_in('rebill_tax_mode', REBILL_TAX_MODES)}", name="rebill_tax_mode"
        ),
        CheckConstraint(
            "amount >= 0 AND amount_claim_ccy >= 0 AND (km IS NULL OR km >= 0) "
            "AND (bill_amount IS NULL OR bill_amount >= 0)",
            name="non_negative",
        ),
        _tenant_fk("claim_id", "expense_claims", "fk_expense_lines_claim_id_expense_claims", "CASCADE"),
        Index("ix_expense_lines_claim_id", "claim_id"),
        Index("ix_expense_lines_expense_date", "expense_date"),
        Index("ix_expense_lines_receipt_sha256", "receipt_sha256"),
    )

    claim_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    km: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    fx_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fx_rate_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_claim_ccy: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    receipt_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stored_files.id", ondelete="RESTRICT"), nullable=True
    )
    receipt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    billable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    reimbursable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    line_status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'pending'"))
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    rebill_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    markup_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    rebill_tax_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bill_amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)


class ExpenseLineDecision(UUIDPk, TenantMixin, _InheritedOrg, Base):
    __tablename__ = "expense_line_decisions"
    __table_args__ = (
        CheckConstraint("decision IN ('approved', 'rejected', 'passed')", name="decision"),
        _tenant_fk("line_id", "expense_lines", "fk_expense_line_decisions_line_id_expense_lines", "CASCADE"),
        _tenant_fk(
            "claim_id", "expense_claims", "fk_expense_line_decisions_claim_id_expense_claims", "CASCADE"
        ),
        Index("ix_expense_line_decisions_line_id", "line_id"),
        Index("ix_expense_line_decisions_claim_id", "claim_id"),
    )

    line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    step: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
