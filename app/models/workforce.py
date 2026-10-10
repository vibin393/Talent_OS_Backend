# ruff: noqa: E501
"""Workforce planning (Epic 36 hub ``workforce_plan_lines``, AD-39; owner Epic 44, branch ``p3_workforce``).

* ``workforce_plans`` (308_01): a plan with its period, currency, lifecycle and origin (``internal`` or a Client
  ``client_forecast``). Client org RLS on ``client_id`` (NULL client = internal only).
* ``workforce_plan_lines`` (hub, ALTERed by 308_01): one row per line **per revision**. ``lineage_id`` is the id of
  the line's first row (stable across revisions; slots and actuals hang off it). ``client_id`` is copied from the
  plan by ``tos_inherit_org`` (never set by callers). ``period_start`` is the planned start month (day 1).
* ``workforce_plan_revisions`` (308_01): revision history and the diff the approver sees. Internal only.
* ``workforce_settings`` (308_01): per-tenant standard hours / days per month and the headcount cap per line.
* ``headcount_slots`` (308_02): one slot per unit of approved headcount, keyed by the line lineage. Internal only.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

PLAN_LINE_STATUSES = ("forecast", "budgeted", "approved", "requisitioned", "cancelled")
# Epic 44
PLAN_STATUSES = ("draft", "budgeted", "pending_approval", "approved", "rejected", "revision_draft")
PLAN_ORIGINS = ("internal", "client_forecast")
LINE_TYPES = ("new", "replacement", "project", "recurring")
WORK_MODES = ("onsite", "remote", "hybrid")
LINE_RATE_UNITS = ("hour", "day", "month", "annual")
CHANGE_KINDS = ("unchanged", "added", "modified", "removed")
REVISION_STATUSES = ("draft", "pending_approval", "approved", "rejected", "discarded")
SLOT_STATUSES = ("open", "requisitioned", "filled", "cancelled")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class WorkforcePlan(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "workforce_plans"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(
            "business_unit_id", "business_units", "fk_workforce_plans_business_unit_id_business_units"
        ),
        _tenant_fk("client_id", "clients", "fk_workforce_plans_client_id_clients"),
        _tenant_fk("project_id", "projects", "fk_workforce_plans_project_id_projects"),
        _tenant_fk("owner_id", "users", "fk_workforce_plans_owner_id_users"),
        _tenant_fk(
            "submitted_by_client_user_id", "client_users", "fk_workforce_plans_submitted_by_client_user_id_cu"
        ),
        CheckConstraint(_in("status", PLAN_STATUSES), name="status"),
        CheckConstraint(_in("origin", PLAN_ORIGINS), name="origin"),
        CheckConstraint(
            "period_end > period_start AND period_end < (period_start + INTERVAL '24 months')::date",
            name="period",
        ),
        CheckConstraint("current_revision >= 0", name="revision"),
        Index("ix_workforce_plans_client_id", "client_id"),
        Index("ix_workforce_plans_owner_id", "owner_id"),
        Index("ix_workforce_plans_business_unit_id", "business_unit_id"),
        Index("ix_workforce_plans_project_id", "project_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_label: Mapped[str | None] = mapped_column(String(20), nullable=True)
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    origin: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'internal'"))
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submitted_by_client_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approved_revision: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    current_revision: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A Client forecast is "picked up" once an internal User edits or transitions it (the Client can no longer).
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkforcePlanLine(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "workforce_plan_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("client_id", "clients", "fk_workforce_plan_lines_client_id_clients"),
        _tenant_fk(
            "project_role_id", "project_roles", "fk_workforce_plan_lines_project_role_id_project_roles"
        ),
        _tenant_fk(
            "workforce_plan_id",
            "workforce_plans",
            "fk_workforce_plan_lines_workforce_plan_id_plans",
            "CASCADE",
        ),
        CheckConstraint(_in("status", PLAN_LINE_STATUSES), name="status"),
        CheckConstraint("headcount >= 1", name="headcount"),
        CheckConstraint("period_end >= period_start", name="period"),
        # Epic 44 (308_01)
        CheckConstraint(_in("line_type", LINE_TYPES), name="line_type"),
        CheckConstraint(f"work_mode IS NULL OR {_in('work_mode', WORK_MODES)}", name="work_mode"),
        CheckConstraint(f"rate_unit IS NULL OR {_in('rate_unit', LINE_RATE_UNITS)}", name="rate_unit"),
        CheckConstraint(_in("change_kind", CHANGE_KINDS), name="change_kind"),
        CheckConstraint(
            "(budget_amount IS NULL OR budget_amount >= 0) AND (bill_rate_amount IS NULL OR bill_rate_amount >= 0)",
            name="money",
        ),
        CheckConstraint("EXTRACT(DAY FROM period_start) = 1", name="start_month"),
        Index("ix_workforce_plan_lines_workforce_plan_id", "workforce_plan_id"),
        Index("ix_workforce_plan_lines_client_id", "client_id"),
        Index("ix_workforce_plan_lines_project_role_id", "project_role_id"),
        Index("ix_workforce_plan_lines_plan_revision", "workforce_plan_id", "revision"),
        Index("ix_workforce_plan_lines_lineage_id", "lineage_id"),
    )

    workforce_plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Inherited from the plan (tos_inherit_org); callers never set it.
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    role_title: Mapped[str] = mapped_column(String(200), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    headcount: Mapped[int] = mapped_column(Integer, nullable=False)
    project_role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'forecast'"))
    # Epic 44 (308_01)
    revision: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    lineage_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # [{skill_key ("global:<uuid>" | "tenant:<uuid>" | null for an unmatched Client name), name, min_proficiency}]
    skills: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    line_type: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'new'"))
    location_text: Mapped[str | None] = mapped_column(String(300), nullable=True)
    work_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    budget_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    bill_rate_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    change_kind: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'added'"))
    supersedes_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The Client's own comment on a forecast line (shown back to the Client; ``notes`` stays internal).
    client_comment: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkforcePlanRevision(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "workforce_plan_revisions"
    __audit_exclude__ = ("diff",)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("workforce_plan_id", "revision_no", name="uq_workforce_plan_revisions_plan_no"),
        _tenant_fk(
            "workforce_plan_id", "workforce_plans", "fk_workforce_plan_revisions_plan_id_plans", "CASCADE"
        ),
        CheckConstraint(_in("status", REVISION_STATUSES), name="status"),
        Index("ix_workforce_plan_revisions_workforce_plan_id", "workforce_plan_id"),
    )

    workforce_plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    diff: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)


class WorkforceSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant, created lazily (``GET/PATCH /workforce/settings``)."""

    __tablename__ = "workforce_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("standard_hours_per_month > 0 AND standard_hours_per_month <= 744", name="hours"),
        CheckConstraint("standard_days_per_month > 0 AND standard_days_per_month <= 31", name="days"),
        CheckConstraint("max_headcount_per_line BETWEEN 1 AND 500", name="max_headcount"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    standard_hours_per_month: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, server_default=text("168")
    )
    standard_days_per_month: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("21")
    )
    max_headcount_per_line: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("500"))


class HeadcountSlot(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "headcount_slots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("plan_line_id", "seq", name="uq_headcount_slots_line_seq"),
        _tenant_fk("workforce_plan_id", "workforce_plans", "fk_headcount_slots_workforce_plan_id_plans"),
        _tenant_fk("plan_line_id", "workforce_plan_lines", "fk_headcount_slots_plan_line_id_lines"),
        _tenant_fk("demand_id", "demands", "fk_headcount_slots_demand_id_demands"),
        _tenant_fk("placement_id", "placements", "fk_headcount_slots_placement_id_placements"),
        CheckConstraint(_in("status", SLOT_STATUSES), name="status"),
        CheckConstraint("seq >= 1", name="seq"),
        Index("ix_headcount_slots_workforce_plan_id", "workforce_plan_id"),
        Index("ix_headcount_slots_plan_line_id", "plan_line_id"),
        Index("ix_headcount_slots_placement_id", "placement_id"),
        Index(
            "uq_headcount_slots_demand_id",
            "demand_id",
            unique=True,
            postgresql_where=text("demand_id IS NOT NULL"),
        ),
    )

    workforce_plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # The line lineage (id of the line's first row; stable across revisions).
    plan_line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False, server_default=text("'open'"))
    demand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requisitioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


__all__ = [
    "CHANGE_KINDS",
    "LINE_RATE_UNITS",
    "LINE_TYPES",
    "PLAN_LINE_STATUSES",
    "PLAN_ORIGINS",
    "PLAN_STATUSES",
    "REVISION_STATUSES",
    "SLOT_STATUSES",
    "WORK_MODES",
    "HeadcountSlot",
    "WorkforcePlan",
    "WorkforcePlanLine",
    "WorkforcePlanRevision",
    "WorkforceSettings",
]
