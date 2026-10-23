# ruff: file-ignore[line-too-long]
"""Timesheet anomaly flags (Epic 63, FR-312; migration ``20261009_410_01_ts_flags``). Owner:
``app.services.timesheet_flags`` (the agent package never touches this table, AD-56).

* ``timesheet_flags`` (audited, ``explanation`` excluded) -- one deterministic rule hit per evaluated timesheet version
  (UNIQUE ``(tenant_id, timesheet_id, timesheet_version, rule)``). Flags never block approval and never change hours;
  ``explanation`` is the plain-English sentence the Timesheet Agent writes (``explanation_source='ai'``) or the rules
  fallback (``rules``). Internal only (tenant RLS + internal-only org RLS): never exposed to Client Users.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

FLAG_RULES = (
    "hours_on_leave",
    "holiday_work",
    "weekend_work",
    "daily_hours_vs_schedule",
    "weekly_overtime_vs_history",
    "peer_outlier",
    "po_hours_cap",
    "zero_hours_period",
    "duplicate_entry_pattern",
)
SEVERITIES = ("info", "warning")
EXPLANATION_SOURCES = ("rules", "ai")
FLAG_STATUSES = ("open", "reviewed", "dismissed", "superseded")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class TimesheetFlag(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "timesheet_flags"
    __audit_exclude__ = ("explanation",)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "timesheet_id", "timesheet_version", "rule", name="uq_timesheet_flags_rule"),
        _tfk("timesheet_id", "timesheets", "fk_timesheet_flags_timesheet_id", "CASCADE"),
        _tfk("placement_id", "placements", "fk_timesheet_flags_placement_id"),
        _tfk("candidate_id", "candidates", "fk_timesheet_flags_candidate_id"),
        CheckConstraint(_in("rule", FLAG_RULES), name="rule"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity"),
        CheckConstraint(_in("explanation_source", EXPLANATION_SOURCES), name="source"),
        CheckConstraint(_in("status", FLAG_STATUSES), name="status"),
        Index("ix_timesheet_flags_timesheet_id", "timesheet_id"),
        Index("ix_timesheet_flags_tenant_status", "tenant_id", "status"),
        Index("ix_timesheet_flags_candidate_id", "candidate_id"),
    )

    timesheet_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    rule: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    entry_dates: Mapped[list[date]] = mapped_column(
        ARRAY(Date), nullable=False, server_default=text("'{}'::date[]")
    )
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    group_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    explanation: Mapped[str | None] = mapped_column(String(600), nullable=True)
    explanation_source: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'rules'"))
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'open'"))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    timesheet_version: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


__all__ = ["EXPLANATION_SOURCES", "FLAG_RULES", "FLAG_STATUSES", "SEVERITIES", "TimesheetFlag"]
