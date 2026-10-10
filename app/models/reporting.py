"""Reporting tables (Epic 11, stories 11.1 / 11.2): ``exports`` (FR-61) and ``user_active_days`` (SM-3).
Tenant-owned with forced RLS.

Migrations: 20261001_111 (tables), 20261001_112 (dashboard indexes on other domains' tables).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

EXPORT_KINDS = ("candidates", "demands", "applications", "submissions", "interviews", "offers")
# Epic 34 (A4): Phase 2 list kinds (migration 20261005_221_02 widens ``ck_exports_kind``).
P2_EXPORT_KINDS = (
    "leads",
    "opportunities",
    "vendors",
    "vendor_submissions",
    "placements",
    "timesheets",
    "expense_claims",
    "invoices",
    "payments",
    "bgv_cases",
)
# Epic 36 (300_01): Phase 3 list kinds (registered centrally by Epic 52).
P3_EXPORT_KINDS = (
    "talent_pools",
    "campaigns",
    "assessments",
    "campus_drives",
    "recruitment_events",
    "projects",
    "immigration_cases",
    "report_definitions",
    "referrals",
    "workforce_plans",
)
# Epic 53 (400_01): Phase 4 list kinds (registered by Epic 65).
P4_EXPORT_KINDS = ("agent_runs", "agent_proposals")
# Epic 66 (500_01): Phase 5 list kinds (registered by Epics 79 / 68 / 69).
P5_EXPORT_KINDS = ("api_usage", "webhook_deliveries", "connector_sync")
EXPORT_KINDS = EXPORT_KINDS + P2_EXPORT_KINDS + P3_EXPORT_KINDS + P4_EXPORT_KINDS + P5_EXPORT_KINDS
EXPORT_FORMATS = ("csv", "xlsx")
EXPORT_STATUSES = ("queued", "running", "done", "failed", "expired")
EXPORT_MODES = ("sync", "async")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Export(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """One export request. Sync exports are recorded ``done`` without a file; async ones get a stored file
    (``file_id``) that only the requester may download until ``expires_at``."""

    __tablename__ = "exports"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "requested_by"],
            ["users.tenant_id", "users.id"],
            name="fk_exports_requested_by_users",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("kind", EXPORT_KINDS), name="kind"),
        CheckConstraint(_in("format", EXPORT_FORMATS), name="format"),
        CheckConstraint(_in("status", EXPORT_STATUSES), name="status"),
        CheckConstraint(_in("mode", EXPORT_MODES), name="mode"),
        Index("ix_exports_requested_by_created", "tenant_id", "requested_by", "created_at"),
        Index("ix_exports_expiry", "tenant_id", "expires_at", postgresql_where=text("status = 'done'")),
    )
    # Filters may carry free-text search terms: kept out of the automatic CRUD audit (``export.run`` records
    # them explicitly, in the same transaction).
    __audit_exclude__ = ("filters",)

    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    format: Mapped[str] = mapped_column(String(5), nullable=False)
    mode: Mapped[str] = mapped_column(String(5), nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    columns: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    masked_columns: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'queued'"))
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserActiveDay(Base):
    """One row per user per local day with an authenticated request (SM-3 weekly active recruiters).
    Written at most once per user per day by the principal resolver; purged after 400 days."""

    __tablename__ = "user_active_days"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_user_active_days_user_id_users",
            ondelete="CASCADE",
        ),
        Index("ix_user_active_days_day", "tenant_id", "day"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)


# ================================================================ Epic 34: summaries (AD-33)
class ReportingFinanceMonthly(Base):
    """Rebuildable monthly finance summary per (LE, month, client) -- derived from posted documents
    (``services/reporting/facts.py``), proven equal to the live facts by ``verify_summaries``. Not audited."""

    __tablename__ = "reporting_finance_monthly"
    __table_args__ = (
        UniqueConstraint("tenant_id", "legal_entity_id", "month", "client_id", name="uq_rpt_fin_monthly_key"),
        Index("ix_rpt_fin_monthly_le_month", "tenant_id", "legal_entity_id", "month"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revenue_inr: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"))
    invoiced_with_tax_inr: Mapped[Decimal] = mapped_column(
        Numeric(16, 2), nullable=False, server_default=text("0")
    )
    cost_inr: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"))
    credit_inr: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"))
    debit_inr: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"))
    invoice_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    credit_note_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    source_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ReportingOpsMonthly(Base):
    """Rebuildable monthly operational counts per (LE, month). Not audited."""

    __tablename__ = "reporting_ops_monthly"
    __table_args__ = (
        UniqueConstraint("tenant_id", "legal_entity_id", "month", name="uq_rpt_ops_monthly_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    workers_on_billing: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    placements_started: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    placements_ended: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    early_terminations: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    positions_closed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    positions_filled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ReportingReconciliationIssue(Base):
    """A summary figure that differed from the live facts (FR-177). ``resolved_at`` is set once a rebuild
    makes them equal again."""

    __tablename__ = "reporting_reconciliation_issues"
    __table_args__ = (Index("ix_rpt_recon_open", "tenant_id", "legal_entity_id", "month", "resolved_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    figure: Mapped[str] = mapped_column(String(40), nullable=False)
    summary_value: Mapped[str | None] = mapped_column(String(40), nullable=True)
    live_value: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "EXPORT_FORMATS",
    "EXPORT_KINDS",
    "EXPORT_MODES",
    "EXPORT_STATUSES",
    "P2_EXPORT_KINDS",
    "P3_EXPORT_KINDS",
    "P4_EXPORT_KINDS",
    "Export",
    "ReportingFinanceMonthly",
    "ReportingOpsMonthly",
    "ReportingReconciliationIssue",
    "UserActiveDay",
]
