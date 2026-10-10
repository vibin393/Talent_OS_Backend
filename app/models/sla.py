"""SLA targets and generic SLA clocks (AD-31; Epic 13 R14).

``sla_targets`` is the Phase 1 table (story 4.6), moved here from ``models/demands.py`` (re-exported there)
and ALTERed by migration 20261002_200_05: ``subject_type`` (default ``demand``), ``scope`` (JSONB, the most
specific match wins), ``calendar`` and ``is_active``. Rows with ``subject_type = 'demand'`` keep their Phase 1
meaning (milestone CHECK, ``priority``, ``clock``); the demand SLA engine reads only those.

``sla_clocks`` are the generic clocks of ``services/sla`` (Phase 2 subjects); the evaluator, notifications
and the demand SLA migration are Epic 32. Both tables are internal-only for org RLS.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

# Phase 1 demand milestones / priorities / clock types (mirrors ``models.demands``).
DEMAND_SLA_MILESTONES = ("assignment", "first_submission", "first_interview", "client_feedback")
DEMAND_PRIORITIES = ("low", "medium", "high", "critical")
SLA_CLOCK_TYPES = ("business", "calendar")
SLA_CALENDARS = ("business", "calendar")
SLA_OUTCOMES = ("met", "breached", "cancelled")
# Epic 32: evaluator status of a clock (``cancelled`` only for a clock cancelled before it breached).
SLA_CLOCK_STATUSES = ("healthy", "at_risk", "breached", "met", "cancelled")
ESCALATION_SUBJECT_KINDS = ("approval_step", "task")
ESCALATION_WATCH_STATUSES = ("active", "done", "cancelled")
NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class SlaTarget(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "sla_targets"
    __table_args__ = (
        # NULLS NOT DISTINCT: at most one "any priority" row per subject type / milestone / scope.
        UniqueConstraint(
            "tenant_id",
            "subject_type",
            "milestone",
            "priority",
            "scope",
            name="uq_sla_targets_subject_milestone_scope",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            f"subject_type <> 'demand' OR {_in('milestone', DEMAND_SLA_MILESTONES)}", name="milestone"
        ),
        CheckConstraint(f"priority IS NULL OR {_in('priority', DEMAND_PRIORITIES)}", name="priority"),
        CheckConstraint(_in("clock", SLA_CLOCK_TYPES), name="clock"),
        CheckConstraint(_in("calendar", SLA_CALENDARS), name="calendar"),
        CheckConstraint("target_hours > 0", name="target_hours"),
        CheckConstraint("at_risk_percent BETWEEN 1 AND 99", name="at_risk_percent"),
    )

    milestone: Mapped[str] = mapped_column(String(30), nullable=False)
    priority: Mapped[str | None] = mapped_column(String(10), nullable=True)
    target_hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    clock: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'business'"))
    at_risk_percent: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("75"))
    # Epic 13 (R14)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("'demand'"))
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=text("'{}'::jsonb"))
    calendar: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'business'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())


class SlaClock(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "sla_clocks"
    __table_args__ = (
        CheckConstraint(f"outcome IS NULL OR {_in('outcome', SLA_OUTCOMES)}", name="outcome"),
        CheckConstraint("(completed_at IS NULL) = (outcome IS NULL)", name="completed_outcome"),
        Index(
            "uq_sla_clocks_running",
            "tenant_id",
            "subject_type",
            "subject_id",
            "milestone",
            text(f"coalesce(ref_id, '{NIL_UUID}'::uuid)"),
            unique=True,
            postgresql_where=text("completed_at IS NULL"),
        ),
        Index("ix_sla_clocks_subject", "subject_type", "subject_id"),
        Index("ix_sla_clocks_open_due", "due_at", postgresql_where=text("completed_at IS NULL")),
        # Epic 32 (migration 20261003_219_01)
        CheckConstraint(_in("status", SLA_CLOCK_STATUSES), name="status"),
        CheckConstraint(
            f"last_notified_status IS NULL OR {_in('last_notified_status', SLA_CLOCK_STATUSES)}",
            name="last_notified_status",
        ),
        Index("ix_sla_clocks_tenant_open", "tenant_id", "completed_at", postgresql_where=text("completed_at IS NULL")),
        Index("ix_sla_clocks_summary", "tenant_id", "subject_type", "milestone", "started_at"),
    )

    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    milestone: Mapped[str] = mapped_column(String(40), nullable=False)
    ref_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    target_hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    calendar: Mapped[str] = mapped_column(String(10), nullable=False)
    at_risk_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    at_risk_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Epic 32: evaluator state + org columns (from ``scope['client_id' | 'vendor_id']``, org RLS).
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'healthy'"))
    last_notified_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    breached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    org_client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    org_vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class EscalationWatch(UUIDPk, TenantMixin, Timestamps, Base):
    """Epic 32 (FR-170): a running escalation on one approval step or task. ``spec`` snapshots the policy
    parameters (explicit approval steps carry their escalation in the request snapshot, without a policy row)."""

    __tablename__ = "escalation_watches"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "subject_kind",
            "subject_id",
            "step_position",
            name="uq_escalation_watches_subject_step",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(_in("subject_kind", ESCALATION_SUBJECT_KINDS), name="subject_kind"),
        CheckConstraint(_in("status", ESCALATION_WATCH_STATUSES), name="status"),
        CheckConstraint("fires >= 0", name="fires"),
        Index("ix_escalation_watches_policy", "policy_id"),
        Index("ix_escalation_watches_due", "next_fire_at", postgresql_where=text("status = 'active'")),
    )

    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("escalation_policies.id", ondelete="SET NULL"), nullable=True
    )
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    step_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assignee_ref: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fires: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    last_outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # [{fire, at, action, outcome, targets}]
    history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
