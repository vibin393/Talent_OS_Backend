# ruff: noqa: E501
"""Epic 31 (Extension, Conversion & Offboarding, FR-162..FR-166) tables, branch ``p2_placement_exit`` (migrations
``20261005_218_01..02``).

* ``placement_exit_settings`` (per tenant), ``placement_end_alerts`` (one row per placement / end date / offset),
  ``placement_end_date_history`` (every end-date change made by this epic).
* ``extensions`` (PRD §5.15): one non-terminal extension per placement (partial UNIQUE). ``client_id`` /
  ``candidate_id`` are denormalised from the placement (inherit trigger) for Client Portal org RLS.
* ``offboarding_templates`` / ``offboarding_cases`` (PRD §5.16, one open case per placement) / ``offboarding_items``
  / ``offboarding_feedback`` and ``conversions`` (one per placement).

``status`` columns are written only through the transition tables in ``services/placements/{extensions,
offboarding}.py`` (AD-4).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

EXTENSION_STATUSES = (
    "proposed",
    "pending_approval",
    "pending_client_confirmation",
    "confirmed",
    "declined",
    "withdrawn",
)
EXTENSION_OPEN = ("proposed", "pending_approval", "pending_client_confirmation")
CLIENT_RESPONSE_KINDS = ("client_user", "secure_link", "recorded_by_user", "user")
EXCEPTION_STATUSES = ("none", "pending", "approved", "rejected")
END_DATE_CAUSES = ("extension", "offboarding", "cancel_offboarding")
CASE_REASONS = ("end_of_term", "client_termination", "worker_resignation", "converted", "misconduct", "other")
CASE_STATUSES = ("initiated", "in_progress", "ready_to_close", "closed", "cancelled")
CASE_OPEN = ("initiated", "in_progress", "ready_to_close")
ITEM_STATUSES = ("pending", "done", "waived", "not_applicable")
COMPLETED_BY_KINDS = ("user", "system", "client_user", "secure_link")
OWNER_ROLES = ("account_manager", "recruiter", "hr", "finance", "payroll")
INITIATED_BY = ("client", "worker")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class PlacementExitSettings(TenantMixin, Timestamps, Base):
    __tablename__ = "placement_exit_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    alert_offsets_days: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[60, 30, 15]'::jsonb")
    )
    notify_client_users: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    bench_excluded_reasons: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text('\'["converted", "misconduct", "worker_resignation"]\'::jsonb'),
    )
    early_attrition_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    auto_case_grace_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class PlacementEndAlert(UUIDPk, TenantMixin, Base):
    __tablename__ = "placement_end_alerts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "placement_id", "end_date", "offset_days", name="uq_placement_end_alerts_key"
        ),
        _tenant_fk("placement_id", "placements", "fk_placement_end_alerts_placement"),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    offset_days: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PlacementEndDateHistory(UUIDPk, TenantMixin, Base):
    __tablename__ = "placement_end_date_history"
    __table_args__ = (
        CheckConstraint(_in("cause", END_DATE_CAUSES), name="cause"),
        _tenant_fk("placement_id", "placements", "fk_placement_end_date_history_placement"),
        Index("ix_placement_end_date_history_placement", "placement_id"),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    old_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    new_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    cause: Mapped[str] = mapped_column(String(30), nullable=False)
    ref_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Extension(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "extensions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", EXTENSION_STATUSES), name="status"),
        CheckConstraint(_in("exception_status", EXCEPTION_STATUSES), name="exception_status"),
        CheckConstraint(
            "client_response_by_kind IS NULL OR " + _in("client_response_by_kind", CLIENT_RESPONSE_KINDS),
            name="client_response_by_kind",
        ),
        CheckConstraint("new_end_date > current_end_date", name="dates"),
        CheckConstraint(
            "(pay_rate IS NULL OR pay_rate >= 0) AND (bill_rate IS NULL OR bill_rate >= 0) "
            "AND (vendor_rate IS NULL OR vendor_rate >= 0)",
            name="rates",
        ),
        _tenant_fk("placement_id", "placements", "fk_extensions_placement"),
        Index("ix_extensions_placement_id", "placement_id"),
        Index(
            "uq_extensions_open",
            "tenant_id",
            "placement_id",
            unique=True,
            postgresql_where=text(_in("status", EXTENSION_OPEN)),
        ),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    current_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    new_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    segment_effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    pay_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    bill_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    vendor_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    burden_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    po_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'proposed'"))
    checks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    exception_approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    exception_status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'none'"))
    exception_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_response_by_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    client_response_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    client_evidence_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stored_files.id", ondelete="RESTRICT"), nullable=True
    )
    client_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rate_segment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    withdraw_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    extension_letter_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    send_extension_letter: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class OffboardingTemplate(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "offboarding_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index(
            "uq_offboarding_templates_default", "tenant_id", unique=True, postgresql_where=text("is_default")
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class OffboardingCase(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "offboarding_cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("reason", CASE_REASONS), name="reason"),
        CheckConstraint(_in("status", CASE_STATUSES), name="status"),
        CheckConstraint(
            "early_termination_by IS NULL OR " + _in("early_termination_by", INITIATED_BY), name="et_by"
        ),
        _tenant_fk("placement_id", "placements", "fk_offboarding_cases_placement"),
        Index("ix_offboarding_cases_placement_id", "placement_id"),
        Index(
            "uq_offboarding_cases_open",
            "tenant_id",
            "placement_id",
            unique=True,
            postgresql_where=text(_in("status", CASE_OPEN)),
        ),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    reason_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    notice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    requested_lwd: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_working_day: Mapped[date] = mapped_column(Date, nullable=False)
    previous_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notice_period_days_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notice_shortfall_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    early_termination_by: Mapped[str | None] = mapped_column(String(10), nullable=True)
    early_attrition: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    bench_eligible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'initiated'"))
    initiated_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_systems: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class OffboardingItem(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "offboarding_items"
    __table_args__ = (
        UniqueConstraint("case_id", "kind", name="uq_offboarding_items_case_kind"),
        CheckConstraint(_in("status", ITEM_STATUSES), name="status"),
        CheckConstraint(
            "completed_by_kind IS NULL OR " + _in("completed_by_kind", COMPLETED_BY_KINDS),
            name="completed_by",
        ),
        CheckConstraint("status <> 'waived' OR waived_reason IS NOT NULL", name="waive_reason"),
        _tenant_fk("case_id", "offboarding_cases", "fk_offboarding_items_case", ondelete="CASCADE"),
        Index("ix_offboarding_items_case_id", "case_id"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    owner_role: Mapped[str | None] = mapped_column(String(30), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    verifier: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    waived_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_by_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    completed_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stored_files.id", ondelete="RESTRICT"), nullable=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    verifier_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class OffboardingFeedback(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "offboarding_feedback"
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_offboarding_feedback_case"),
        _tenant_fk("case_id", "offboarding_cases", "fk_offboarding_feedback_case", ondelete="CASCADE"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    recorded_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class Conversion(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "conversions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "placement_id", name="uq_conversions_placement"),
        CheckConstraint("offered_compensation >= 0", name="comp"),
        _tenant_fk("placement_id", "placements", "fk_conversions_placement"),
        _tenant_fk("offboarding_case_id", "offboarding_cases", "fk_conversions_case"),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    conversion_date: Mapped[date] = mapped_column(Date, nullable=False)
    offered_compensation: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    fee_rule_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    fee_proposal: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    offboarding_case_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
