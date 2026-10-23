# ruff: file-ignore[line-too-long]
"""HRMS sync (Epic 71, stories 71.1-71.3, FR-365..FR-367; migration ``<date>_505_01_hrms_sync``). Owner:
``app.services.hrms_sync``.

* ``hrms_field_mappings`` -- per Installation x Legal Entity: HRMS field <- Talent OS catalogue key / constant (audited).
  Sensitive rows (PAN, bank, Aadhaar reference) need a recorded purpose and the enabling Finance user.
* ``hrms_sync_records`` -- one row per (Installation, Placement): push state, employee ref / code and the daily pull
  result. Ids, statuses and sanitised messages only (no field values; erasure handler ``hrms_sync.records``).
* ``hrms_settings`` -- one row per tenant (audited).
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

TRANSFORMS = ("none", "date_iso", "date_dmy", "upper", "lower", "lookup")
PUSH_KINDS = ("hire", "change", "exit")
RECORD_STATUSES = ("pending", "blocked", "synced", "failed", "skipped")
HRMS_STATUSES = ("active", "exited", "pending", "unknown")
MISMATCHES = ("none", "status", "exit_date", "both")
PUSH_WORKER_TYPES = ("employee", "contractor")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


class HrmsFieldMapping(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "hrms_field_mappings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "installation_id", "legal_entity_id", "hrms_field", name="uq_hrms_field_mappings_field"),
        _fk("installation_id", "app_installations", "fk_hrms_field_mappings_installation", "CASCADE"),
        _fk("legal_entity_id", "legal_entities", "fk_hrms_field_mappings_legal_entity"),
        _fk("enabled_by", "users", "fk_hrms_field_mappings_enabled_by"),
        CheckConstraint(_in("transform", TRANSFORMS), name="transform"),
        CheckConstraint("local_field IS NOT NULL OR constant IS NOT NULL", name="source"),
        CheckConstraint("NOT sensitive OR (purpose IS NOT NULL AND enabled_by IS NOT NULL)", name="sensitive_purpose"),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    hrms_field: Mapped[str] = mapped_column(String(100), nullable=False)
    local_field: Mapped[str | None] = mapped_column(String(60), nullable=True)
    constant: Mapped[str | None] = mapped_column(String(200), nullable=True)
    transform: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'none'"))
    lookup: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    purpose: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HrmsSyncRecord(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "hrms_sync_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "installation_id", "placement_id", name="uq_hrms_sync_records_placement"),
        _fk("installation_id", "app_installations", "fk_hrms_sync_records_installation", "CASCADE"),
        _fk("placement_id", "placements", "fk_hrms_sync_records_placement", "CASCADE"),
        CheckConstraint(f"last_push_kind IS NULL OR {_in('last_push_kind', PUSH_KINDS)}", name="last_push_kind"),
        CheckConstraint(_in("status", RECORD_STATUSES), name="status"),
        CheckConstraint(f"hrms_status IS NULL OR {_in('hrms_status', HRMS_STATUSES)}", name="hrms_status"),
        CheckConstraint(_in("mismatch", MISMATCHES), name="mismatch"),
        Index("ix_hrms_sync_records_status", "status"),
        Index("ix_hrms_sync_records_candidate_id", "candidate_id"),
        Index("ix_hrms_sync_records_mismatch", "mismatch", postgresql_where=text("mismatch <> 'none'")),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    include_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    employee_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    employee_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    pushed_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_push_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'pending'"))
    blocked_fields: Mapped[list[str]] = mapped_column(ARRAY(String(100)), nullable=False, server_default=text("'{}'::varchar(100)[]"))
    skip_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hrms_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    hrms_exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_pulled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mismatch: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'none'"))
    mismatch_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    field_hashes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class HrmsSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "hrms_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", name="uq_hrms_settings_tenant_id"),
        CheckConstraint("push_worker_types <@ ARRAY['employee','contractor']::varchar(20)[]", name="worker_types"),
        CheckConstraint("pull_hour_ist BETWEEN 0 AND 23", name="pull_hour"),
    )

    push_worker_types: Mapped[list[str]] = mapped_column(ARRAY(String(20)), nullable=False, server_default=text("'{employee}'::varchar(20)[]"))
    push_on_join: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    push_changes: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    push_exits: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    pull_hour_ist: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("6"))
