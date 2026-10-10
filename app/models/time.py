"""Time domain tables (Epic 25 Timesheets; Epic 26 Leave appends its tables here). Branch ``p2_timesheets``,
migrations ``20261020_212_01..03``.

* ``time_settings`` (one row per tenant, lazy), ``timesheet_config_defaults`` (per Client) and
  ``timesheet_configs`` (per Placement, versioned: a change writes a new version effective from the first
  period not yet submitted, FR-128). ``holiday_calendar_id`` has no FK here -- the Leave branch adds it.
* ``timesheets`` / ``timesheet_entries``: org columns are copied from the placement (immutable) and drive org
  RLS (client, vendor and worker sessions see only their own rows). ``status`` is written only by
  ``time.timesheets.transition_timesheet`` (AD-4). Entries are immutable while the parent is not
  ``draft`` / ``rejected`` and while a non-released posting exists (``tos_immutable`` + ``tos_source_lock``).
* ``timesheet_adjustment_entries`` (signed deltas of Adjustment Timesheets), ``rate_adjustments`` (retro rate
  deltas on posted hours, AD-23) and ``timesheet_reminders_sent`` (FR-133 rate limit).

Hours are NUMERIC(8,2), multipliers NUMERIC(14,4), rate deltas NUMERIC(14,4) (AD-20). No money is stored.
"""

import uuid
from datetime import date, datetime, time
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
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk
from app.schemas.time import ENTRY_MODES, PERIOD_KINDS, TIME_CATEGORIES, TIMESHEET_STATUSES

TIMESHEET_KINDS = ("regular", "adjustment")
LEDGERS = ("billing", "payable")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


_DEFAULT_CATEGORIES = "'{regular,ot,double_time,holiday,leave_paid,leave_unpaid}'::text[]"


class _ConfigColumns:
    period_kind: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'weekly'"))
    week_start: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    entry_mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'hours'"))
    hours_per_day: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, server_default=text("8.00"))
    work_days: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False, server_default=text("'{0,1,2,3,4}'::smallint[]")
    )
    enabled_categories: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text(_DEFAULT_CATEGORIES)
    )
    ot_daily_threshold: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    dt_daily_threshold: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    ot_weekly_threshold: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    pay_multipliers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    bill_multipliers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    daily_cap: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, server_default=text("24.00"))
    prefill_holidays: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    prefill_leave: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    holiday_calendar_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approval_route: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


def _config_checks() -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint(_in("period_kind", PERIOD_KINDS), name="period_kind"),
        CheckConstraint(_in("entry_mode", ENTRY_MODES), name="entry_mode"),
        CheckConstraint("week_start BETWEEN 0 AND 6", name="week_start"),
        CheckConstraint("hours_per_day > 0 AND hours_per_day <= 24", name="hours_per_day"),
        CheckConstraint("daily_cap > 0 AND daily_cap <= 24", name="daily_cap"),
        CheckConstraint(
            "(ot_daily_threshold IS NULL OR ot_daily_threshold >= 0) "
            "AND (dt_daily_threshold IS NULL OR dt_daily_threshold >= 0) "
            "AND (ot_weekly_threshold IS NULL OR ot_weekly_threshold >= 0)",
            name="thresholds",
        ),
    )


class TimeSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant, created lazily (``GET/PATCH /time/settings``)."""

    __tablename__ = "time_settings"
    __audit_entity_id__ = "tenant_id"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    missing_grace_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    reminder_offsets: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{0,1}'::int[]")
    )
    copy_previous_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    import_allow_client_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    allow_empty_route: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class TimesheetConfigDefault(UUIDPk, TenantMixin, Timestamps, AuditedMixin, _ConfigColumns, Base):
    __tablename__ = "timesheet_config_defaults"
    __table_args__ = (
        UniqueConstraint("tenant_id", "client_id"),
        *_config_checks(),
        _tenant_fk("client_id", "clients", "fk_timesheet_config_defaults_client_id_clients", "CASCADE"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class TimesheetConfig(UUIDPk, TenantMixin, AuditedMixin, _ConfigColumns, Base):
    """Versioned per Placement; rows are never updated (a change is a new version)."""

    __tablename__ = "timesheet_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("placement_id", "version"),
        *_config_checks(),
        _tenant_fk("placement_id", "placements", "fk_timesheet_configs_placement_id_placements", "CASCADE"),
        Index("ix_timesheet_configs_placement_id", "placement_id", "version"),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    # org columns copied from the placement (``tos_inherit_org``)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Timesheet(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "timesheets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        UniqueConstraint(
            "tenant_id", "placement_id", "period_start", "kind", "adjustment_seq", name="uq_timesheets_period"
        ),
        CheckConstraint(_in("kind", TIMESHEET_KINDS), name="kind"),
        CheckConstraint(_in("status", TIMESHEET_STATUSES), name="status"),
        CheckConstraint("period_end >= period_start", name="period"),
        CheckConstraint(
            "(kind = 'regular' AND adjustment_seq = 0 AND original_timesheet_id IS NULL) "
            "OR (kind = 'adjustment' AND adjustment_seq > 0 AND original_timesheet_id IS NOT NULL)",
            name="adjustment",
        ),
        _tenant_fk("placement_id", "placements", "fk_timesheets_placement_id_placements"),
        _tenant_fk("candidate_id", "candidates", "fk_timesheets_candidate_id_candidates"),
        _tenant_fk("client_id", "clients", "fk_timesheets_client_id_clients"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_timesheets_legal_entity_id_legal_entities"),
        _tenant_fk("original_timesheet_id", "timesheets", "fk_timesheets_original_timesheet_id_timesheets"),
        Index("ix_timesheets_placement_id", "placement_id", "period_start"),
        Index("ix_timesheets_candidate_id", "candidate_id"),
        Index("ix_timesheets_client_id", "client_id"),
        Index("ix_timesheets_legal_entity_id", "legal_entity_id", "period_start"),
        Index("ix_timesheets_status", "status"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'regular'"))
    adjustment_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    original_timesheet_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'draft'"))
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_by_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    submitted_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    proxy_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("approval_requests.id", ondelete="SET NULL"), nullable=True
    )
    client_approval_evidence_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stored_files.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_step: Mapped[str | None] = mapped_column(String(100), nullable=True)
    missing_flagged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class _InheritedOrg:
    """Org columns copied from the parent timesheet by ``tos_inherit_org``."""

    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class TimesheetEntry(UUIDPk, TenantMixin, Timestamps, _InheritedOrg, Base):
    __tablename__ = "timesheet_entries"
    __table_args__ = (
        UniqueConstraint("timesheet_id", "entry_date", "category", name="uq_timesheet_entries_cell"),
        CheckConstraint(_in("category", TIME_CATEGORIES), name="category"),
        CheckConstraint("hours >= 0 AND hours <= 24", name="hours"),
        CheckConstraint(
            "entered_hours IS NULL OR (entered_hours >= 0 AND entered_hours <= 24)", name="entered"
        ),
        CheckConstraint("break_minutes IS NULL OR break_minutes >= 0", name="break_minutes"),
        _tenant_fk("timesheet_id", "timesheets", "fk_timesheet_entries_timesheet_id_timesheets", "CASCADE"),
        Index("ix_timesheet_entries_timesheet_id", "timesheet_id"),
        Index("ix_timesheet_entries_entry_date", "entry_date"),
    )

    timesheet_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    entered_hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leave_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    holiday_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    prefilled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    prefill_deletable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_proxy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class TimesheetAdjustmentEntry(UUIDPk, TenantMixin, Timestamps, _InheritedOrg, Base):
    __tablename__ = "timesheet_adjustment_entries"
    __table_args__ = (
        UniqueConstraint(
            "timesheet_id", "entry_date", "category", name="uq_timesheet_adjustment_entries_cell"
        ),
        CheckConstraint(_in("category", TIME_CATEGORIES), name="category"),
        CheckConstraint("delta_hours <> 0 AND delta_hours BETWEEN -24 AND 24", name="delta"),
        _tenant_fk(
            "timesheet_id", "timesheets", "fk_timesheet_adjustment_entries_timesheet_id_timesheets", "CASCADE"
        ),
        _tenant_fk(
            "original_timesheet_id", "timesheets", "fk_timesheet_adjustment_entries_original_timesheets"
        ),
        Index("ix_timesheet_adjustment_entries_timesheet_id", "timesheet_id"),
        Index("ix_timesheet_adjustment_entries_original", "original_timesheet_id", "entry_date"),
    )

    timesheet_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    original_timesheet_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    delta_hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class RateAdjustment(UUIDPk, TenantMixin, Base):
    """A retro rate delta on a posted source item (AD-23): ``quantity x rate_delta x multiplier`` is priced by
    the consumer (billing / payroll). Idempotent per (ledger, source, segment). Internal only."""

    __tablename__ = "rate_adjustments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "ledger",
            "original_source_kind",
            "original_source_id",
            "segment_id",
            name="uq_rate_adjustments_source_segment",
        ),
        CheckConstraint(_in("ledger", LEDGERS), name="ledger"),
        CheckConstraint("rate_delta <> 0", name="rate_delta"),
        _tenant_fk("placement_id", "placements", "fk_rate_adjustments_placement_id_placements"),
        Index("ix_rate_adjustments_source", "original_source_kind", "original_source_id"),
        Index("ix_rate_adjustments_placement_id", "placement_id", "item_date"),
    )

    ledger: Mapped[str] = mapped_column(String(10), nullable=False)
    original_source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    original_source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    item_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    base_rate: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    new_rate: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    rate_delta: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    segment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class TimesheetReminderSent(UUIDPk, TenantMixin, Base):
    __tablename__ = "timesheet_reminders_sent"
    __table_args__ = (
        _tenant_fk(
            "timesheet_id", "timesheets", "fk_timesheet_reminders_sent_timesheet_id_timesheets", "CASCADE"
        ),
        Index("ix_timesheet_reminders_sent_timesheet_id", "timesheet_id", "sent_at"),
    )

    timesheet_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # manual | scheduled
    recipient_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# ================================================================ Epic 26: Leave & Holidays (branch ``p2_leave``)
# Migrations ``20261005_213_01..03``. Days are NUMERIC(6,2) (AD-20). The balance ledger is append-only
# (``tos_immutable`` without mutable states; only ``reason`` may be cleared by erasure); ``leave_balances`` is the
# per-(worker, type, legal entity, leave year) cache row locked ``FOR UPDATE`` on every ledger write.
HOLIDAY_SCOPE_KINDS = ("country", "state", "client", "legal_entity")
HOLIDAY_TYPES = ("national", "state", "optional", "client")
ACCRUAL_MODES = ("monthly", "annual_grant", "none")
LEAVE_ENTRY_KINDS = (
    "accrual",
    "grant",
    "adjustment",
    "request_debit",
    "request_credit",
    "carry_forward",
    "lapse",
    "opening",
)
LEAVE_STATUSES = ("requested", "approved", "rejected", "withdrawn", "cancellation_requested", "cancelled")
LEAVE_CLIENT_MODES = ("none", "notify", "approve")


class HolidayCalendar(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "holiday_calendars"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("scope_kind", HOLIDAY_SCOPE_KINDS), name="scope_kind"),
        CheckConstraint(
            "(scope_kind <> 'client' OR client_id IS NOT NULL) "
            "AND (scope_kind <> 'legal_entity' OR legal_entity_id IS NOT NULL) "
            "AND (scope_kind <> 'state' OR state_code IS NOT NULL)",
            name="scope_ref",
        ),
        CheckConstraint("year BETWEEN 2000 AND 2100", name="year"),
        CheckConstraint("optional_allowance >= 0", name="optional_allowance"),
        _tenant_fk("client_id", "clients", "fk_holiday_calendars_client_id_clients"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_holiday_calendars_legal_entity_id_legal_entities"),
        Index("ix_holiday_calendars_year", "year", "scope_kind"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    country_code: Mapped[str] = mapped_column(CHAR(2), nullable=False, server_default=text("'IN'"))
    state_code: Mapped[str | None] = mapped_column(
        CHAR(2), ForeignKey("gst_states.code", ondelete="RESTRICT"), nullable=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    optional_allowance: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    copied_from_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Holiday(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "holidays"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("calendar_id", "holiday_date", "name", name="uq_holidays_calendar_date_name"),
        CheckConstraint(_in("type", HOLIDAY_TYPES), name="type"),
        _tenant_fk("calendar_id", "holiday_calendars", "fk_holidays_calendar_id_holiday_calendars", "CASCADE"),
        Index("ix_holidays_calendar_id", "calendar_id", "holiday_date"),
    )

    calendar_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'national'"))


class WorkerOptionalHoliday(UUIDPk, TenantMixin, Base):
    __tablename__ = "worker_optional_holidays"
    __table_args__ = (
        UniqueConstraint("placement_id", "holiday_id", name="uq_worker_optional_holidays_pick"),
        _tenant_fk("candidate_id", "candidates", "fk_worker_optional_holidays_candidate_id_candidates"),
        _tenant_fk(
            "placement_id", "placements", "fk_worker_optional_holidays_placement_id_placements", "CASCADE"
        ),
        _tenant_fk("holiday_id", "holidays", "fk_worker_optional_holidays_holiday_id_holidays", "CASCADE"),
        Index("ix_worker_optional_holidays_candidate_id", "candidate_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    holiday_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class LeaveSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant, created lazily."""

    __tablename__ = "leave_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("leave_year IN ('calendar', 'fiscal')", name="leave_year"),
        CheckConstraint("accrual_day BETWEEN 1 AND 28", name="accrual_day"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    leave_year: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'calendar'"))
    default_work_week: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False, server_default=text("'{0,1,2,3,4}'::smallint[]")
    )
    accrual_day: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    client_team_calendar_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class LeaveClientSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """Per-Client leave options: ``leave_client_mode`` and the team calendar opt-in (NULL = tenant default)."""

    __tablename__ = "leave_client_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "client_id"),
        CheckConstraint(_in("leave_client_mode", LEAVE_CLIENT_MODES), name="leave_client_mode"),
        _tenant_fk("client_id", "clients", "fk_leave_client_settings_client_id_clients", "CASCADE"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    leave_client_mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'none'"))
    client_team_calendar: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class LeaveType(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "leave_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "code"),
        CheckConstraint(_in("accrual_mode", ACCRUAL_MODES), name="accrual_mode"),
        CheckConstraint("timesheet_category IN ('leave_paid', 'leave_unpaid')", name="timesheet_category"),
        CheckConstraint("accrual_amount >= 0", name="accrual_amount"),
        CheckConstraint("cap IS NULL OR cap >= 0", name="cap"),
        CheckConstraint("carry_forward_limit IS NULL OR carry_forward_limit >= 0", name="carry_forward_limit"),
        CheckConstraint("negative_limit IS NULL OR negative_limit >= 0", name="negative_limit"),
    )

    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    paid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    billable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    accrual_mode: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'none'"))
    accrual_amount: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, server_default=text("0"))
    cap: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    carry_forward_limit: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    allow_negative: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    negative_limit: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    allow_half_day: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    eligible_worker_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    eligible_legal_entity_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    timesheet_category: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'leave_paid'")
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LeaveTypeClientOverride(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "leave_type_client_overrides"
    __table_args__ = (
        UniqueConstraint("leave_type_id", "client_id", name="uq_leave_type_client_overrides_pair"),
        _tenant_fk("leave_type_id", "leave_types", "fk_leave_type_client_overrides_leave_type_id", "CASCADE"),
        _tenant_fk("client_id", "clients", "fk_leave_type_client_overrides_client_id_clients", "CASCADE"),
    )

    leave_type_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    accrual_amount: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    cap: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    billable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    requires_client_approval: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class LeaveBalanceEntry(UUIDPk, TenantMixin, Base):
    """Append-only ledger row (corrections are new ``adjustment`` rows)."""

    __tablename__ = "leave_balance_entries"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "leave_type_id",
            "legal_entity_id",
            "source_ref",
            name="uq_leave_balance_entries_source",
        ),
        CheckConstraint(_in("entry_kind", LEAVE_ENTRY_KINDS), name="entry_kind"),
        CheckConstraint("days <> 0", name="days"),
        _tenant_fk("candidate_id", "candidates", "fk_leave_balance_entries_candidate_id_candidates"),
        _tenant_fk("leave_type_id", "leave_types", "fk_leave_balance_entries_leave_type_id_leave_types"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_leave_balance_entries_legal_entity_id"),
        Index("ix_leave_balance_entries_key", "candidate_id", "leave_type_id", "legal_entity_id", "leave_year"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    leave_type_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    leave_year: Mapped[str] = mapped_column(String(10), nullable=False)
    entry_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    days: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'system'"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class LeaveBalance(UUIDPk, TenantMixin, Timestamps, Base):
    """Cache of the ledger sum (``balance``) plus the days of open requests (``pending``)."""

    __tablename__ = "leave_balances"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "leave_type_id",
            "legal_entity_id",
            "leave_year",
            name="uq_leave_balances_key",
        ),
        CheckConstraint("pending >= 0", name="pending"),
        _tenant_fk("candidate_id", "candidates", "fk_leave_balances_candidate_id_candidates"),
        _tenant_fk("leave_type_id", "leave_types", "fk_leave_balances_leave_type_id_leave_types"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_leave_balances_legal_entity_id_legal_entities"),
        Index("ix_leave_balances_candidate_id", "candidate_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    leave_type_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    leave_year: Mapped[str] = mapped_column(String(10), nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, server_default=text("0"))
    pending: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, server_default=text("0"))


class LeaveRequest(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """``status`` is written only by ``time.leave_requests.transition_leave`` (AD-4)."""

    __tablename__ = "leave_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", LEAVE_STATUSES), name="status"),
        CheckConstraint("end_date >= start_date", name="dates"),
        CheckConstraint("start_half IS NULL OR start_half = 'pm'", name="start_half"),
        CheckConstraint("end_half IS NULL OR end_half = 'am'", name="end_half"),
        CheckConstraint("days > 0", name="days"),
        _tenant_fk("candidate_id", "candidates", "fk_leave_requests_candidate_id_candidates"),
        _tenant_fk("placement_id", "placements", "fk_leave_requests_placement_id_placements"),
        _tenant_fk("client_id", "clients", "fk_leave_requests_client_id_clients"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_leave_requests_legal_entity_id_legal_entities"),
        _tenant_fk("leave_type_id", "leave_types", "fk_leave_requests_leave_type_id_leave_types"),
        Index("ix_leave_requests_candidate_id", "candidate_id", "start_date"),
        Index("ix_leave_requests_placement_id", "placement_id", "start_date"),
        Index("ix_leave_requests_client_id", "client_id", "start_date"),
        Index("ix_leave_requests_status", "status"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    leave_type_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    leave_year: Mapped[str] = mapped_column(String(10), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_half: Mapped[str | None] = mapped_column(String(2), nullable=True)
    end_half: Mapped[str | None] = mapped_column(String(2), nullable=True)
    days: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'requested'"))
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("approval_requests.id", ondelete="SET NULL"), nullable=True
    )
    requested_by_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'candidate'")
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    proxy_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
