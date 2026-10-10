# ruff: noqa: E501
"""Custom report builder & scheduled reports (Epic 47, FR-237..FR-240; branch ``p3_reports``, band 311).

* ``report_definitions`` -- a saved foundation ``Definition`` (``definition`` JSONB, stored as-is) plus the
  report-only ``viz`` JSON; ``definition_version`` counts the saved versions (``report_definition_versions`` is
  append-only; a run records the version it used). ``status='needs_repair'`` + ``repair_issues`` when the
  registry no longer knows a referenced field / join / operator.
* ``report_shares`` -- team / role / tenant targets (one CHECK per kind); ``report_pins`` -- a user's personal
  dashboard pins.
* ``report_schedules`` / ``report_runs`` -- scheduled per-recipient runs; ``(schedule_id, slot_at,
  principal_user_id)`` is unique so a re-delivered slot is a no-op.
* ``report_builder_settings`` -- one row per tenant (caps may only be *lowered* below the platform maximums).

No table holds subject data (definitions name registered keys only); generated files follow the export retention.
"""

import uuid
from datetime import datetime, time
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

VISIBILITIES = ("private", "shared")
REPORT_STATUSES = ("ok", "needs_repair")
SHARE_KINDS = ("team", "role", "tenant")
CADENCES = ("daily", "weekly", "monthly")
SCHEDULE_FORMATS = ("xlsx", "csv", "pdf")
SCHEDULE_STATUSES = ("active", "paused")
RUN_KINDS = ("preview", "run", "export", "scheduled")
RUN_STATUSES = ("queued", "running", "done", "failed", "limit_hit", "skipped")
LIMIT_KINDS = ("row_cap", "timeout")
DELIVERIES = ("attachment", "link", "none")
VIZ_TYPES = ("table", "pivot", "bar", "line", "pie", "kpi")

# Platform maximums (a tenant may lower them, never raise them).
PLATFORM_PREVIEW_ROWS = 500
PLATFORM_EXPORT_ROW_CAP = 100_000
PLATFORM_TIMEOUT_S = 30


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class ReportBuilderSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant, created lazily."""

    __tablename__ = "report_builder_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint(f"preview_rows BETWEEN 1 AND {PLATFORM_PREVIEW_ROWS}", name="preview_rows"),
        CheckConstraint(f"export_row_cap BETWEEN 1 AND {PLATFORM_EXPORT_ROW_CAP}", name="export_row_cap"),
        CheckConstraint(f"statement_timeout_s BETWEEN 1 AND {PLATFORM_TIMEOUT_S}", name="timeout"),
        CheckConstraint("attachment_max_mb BETWEEN 1 AND 25", name="attachment_max_mb"),
        CheckConstraint("link_ttl_days BETWEEN 1 AND 30", name="link_ttl_days"),
        CheckConstraint("max_recipients BETWEEN 1 AND 50", name="max_recipients"),
        CheckConstraint("max_schedules BETWEEN 1 AND 500", name="max_schedules"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    preview_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("500"))
    export_row_cap: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100000"))
    statement_timeout_s: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    attachment_max_mb: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    link_ttl_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("7"))
    max_recipients: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("50"))
    max_schedules: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("500"))


class ReportDefinition(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "report_definitions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("owner_id", "users", "fk_report_definitions_owner_id_users"),
        CheckConstraint(_in("visibility", VISIBILITIES), name="visibility"),
        CheckConstraint(_in("status", REPORT_STATUSES), name="status"),
        CheckConstraint("definition_version >= 1", name="version"),
        Index("ix_report_definitions_owner_id", "owner_id"),
        Index("ix_report_definitions_source_key", "tenant_id", "source_key"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_key: Mapped[str] = mapped_column(String(80), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    definition_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    viz: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'private'"))
    status: Mapped[str] = mapped_column(String(15), nullable=False, server_default=text("'ok'"))
    repair_issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    repair_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReportDefinitionVersion(UUIDPk, TenantMixin, AuditedMixin, Base):
    """Append-only history of a report's definition + viz."""

    __tablename__ = "report_definition_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("report_id", "version", name="uq_report_definition_versions_report_version"),
        _tenant_fk("report_id", "report_definitions", "fk_report_definition_versions_report_id"),
        _tenant_fk("created_by", "users", "fk_report_definition_versions_created_by_users"),
    )

    report_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    viz: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReportShare(UUIDPk, TenantMixin, AuditedMixin, Base):
    __tablename__ = "report_shares"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("report_id", "report_definitions", "fk_report_shares_report_id", ondelete="CASCADE"),
        _tenant_fk("team_id", "teams", "fk_report_shares_team_id_teams", ondelete="CASCADE"),
        _tenant_fk("role_id", "roles", "fk_report_shares_role_id_roles", ondelete="CASCADE"),
        _tenant_fk("created_by", "users", "fk_report_shares_created_by_users"),
        CheckConstraint(_in("target_kind", SHARE_KINDS), name="target_kind"),
        CheckConstraint(
            "(target_kind = 'team' AND team_id IS NOT NULL AND role_id IS NULL)"
            " OR (target_kind = 'role' AND role_id IS NOT NULL AND team_id IS NULL)"
            " OR (target_kind = 'tenant' AND team_id IS NULL AND role_id IS NULL)",
            name="target",
        ),
        Index(
            "uq_report_shares_target",
            "report_id",
            "target_kind",
            text("COALESCE(team_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            text("COALESCE(role_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
        ),
    )

    report_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReportPin(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "report_pins"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("user_id", "report_id", name="uq_report_pins_user_report"),
        _tenant_fk("user_id", "users", "fk_report_pins_user_id_users", ondelete="CASCADE"),
        _tenant_fk("report_id", "report_definitions", "fk_report_pins_report_id", ondelete="CASCADE"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    report_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    viz_override: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class ReportSchedule(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "report_schedules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("report_id", "report_definitions", "fk_report_schedules_report_id", ondelete="CASCADE"),
        _tenant_fk("owner_id", "users", "fk_report_schedules_owner_id_users"),
        CheckConstraint(_in("cadence", CADENCES), name="cadence"),
        CheckConstraint(_in("format", SCHEDULE_FORMATS), name="format"),
        CheckConstraint(_in("status", SCHEDULE_STATUSES), name="status"),
        CheckConstraint(
            "(cadence = 'weekly') = (weekday IS NOT NULL) AND (weekday IS NULL OR weekday BETWEEN 0 AND 6)",
            name="weekday",
        ),
        CheckConstraint(
            "(cadence = 'monthly') = (month_day IS NOT NULL)"
            " AND (month_day IS NULL OR month_day = -1 OR month_day BETWEEN 1 AND 28)",
            name="month_day",
        ),
        CheckConstraint("cardinality(recipient_user_ids) BETWEEN 1 AND 50", name="recipients"),
        CheckConstraint("consecutive_failures >= 0", name="failures"),
        Index("ix_report_schedules_report_id", "report_id"),
        Index("ix_report_schedules_due", "status", "next_run_at"),
    )

    report_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    cadence: Mapped[str] = mapped_column(String(10), nullable=False)
    weekday: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # 0 = Monday
    month_day: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # 1..28, -1 = last day
    local_time: Mapped[time] = mapped_column(Time, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'Asia/Kolkata'"))
    format: Mapped[str] = mapped_column(String(5), nullable=False)
    recipient_user_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid(as_uuid=True)), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    paused_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReportRun(UUIDPk, TenantMixin, Base):
    """One preview / run / export / scheduled delivery (audited explicitly with ``report.<action>``)."""

    __tablename__ = "report_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("report_id", "report_definitions", "fk_report_runs_report_id", ondelete="CASCADE"),
        _tenant_fk("schedule_id", "report_schedules", "fk_report_runs_schedule_id", ondelete="SET NULL"),
        _tenant_fk("principal_user_id", "users", "fk_report_runs_principal_user_id_users"),
        _tenant_fk("file_id", "stored_files", "fk_report_runs_file_id_stored_files", ondelete="SET NULL"),
        CheckConstraint(_in("run_kind", RUN_KINDS), name="run_kind"),
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        CheckConstraint("limit_kind IS NULL OR " + _in("limit_kind", LIMIT_KINDS), name="limit_kind"),
        CheckConstraint("format IS NULL OR " + _in("format", SCHEDULE_FORMATS), name="format"),
        CheckConstraint(_in("delivery", DELIVERIES), name="delivery"),
        CheckConstraint("(status = 'limit_hit') = (limit_kind IS NOT NULL)", name="limit_hit"),
        Index("ix_report_runs_report_id", "report_id", "started_at"),
        Index("ix_report_runs_principal", "principal_user_id", "started_at"),
        Index("ix_report_runs_expires", "expires_at"),
        Index(
            "uq_report_runs_slot",
            "schedule_id",
            "slot_at",
            "principal_user_id",
            unique=True,
            postgresql_where=text("schedule_id IS NOT NULL"),
        ),
    )

    report_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )  # NULL = unsaved preview
    source_key: Mapped[str] = mapped_column(String(80), nullable=False)
    definition_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    slot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    principal_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'running'"))
    limit_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    masked_fields: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    format: Mapped[str | None] = mapped_column(String(5), nullable=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    delivery: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'none'"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
