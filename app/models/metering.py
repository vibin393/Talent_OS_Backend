# ruff: file-ignore[line-too-long]
"""Usage metering (Epic 66, AD-78, FR-338, NFR-71/74; migration ``20261009_500_03_p5_platform``). Owner:
``app.services.metering``.

* ``usage_counters`` (tenant, not audited): daily rollup per unit / Installation / API key / route group, upserted by
  ``metering.flush`` from the Redis hot-path counters. Informational only (D-12).
* ``api_request_log`` (tenant, retention 30 days): one row per Public API / MCP request; **range-partitioned by month
  on ``at``** (PK ``(id, at)``); RLS on the parent, partitions inherit it. The P1 ``api_key_request_log`` stays for
  internal ``/api/v1`` key calls.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, Timestamps, UUIDPk

USAGE_UNITS = (
    "api_request", "webhook_attempt", "mcp_tool_call", "connector_job", "job_board_posting", "telephony_minute",
    "transcription_minute", "portal_copilot_question",
)  # fmt: skip


class UsageCounter(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "usage_counters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id",
            "day",
            "unit",
            "installation_id",
            "api_key_id",
            "route_group",
            name="uq_usage_counters_key",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(f"unit IN ({', '.join(repr(u) for u in USAGE_UNITS)})", name="unit"),
        Index("ix_usage_counters_app_id_day", "app_id", "day"),
    )

    day: Mapped[date] = mapped_column(Date(), nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    app_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    route_group: Mapped[str] = mapped_column(String(60), nullable=False, server_default=text("''"))
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, server_default=text("0"))


class ApiRequestLog(TenantMixin, Base):
    __tablename__ = "api_request_log"
    __table_args__ = (
        CheckConstraint("installation_id IS NOT NULL OR api_key_id IS NOT NULL", name="caller"),
        Index("ix_api_request_log_installation_id_at", "installation_id", "at"),
        Index("ix_api_request_log_api_key_id_at", "api_key_id", "at"),
        Index("ix_api_request_log_at", "at"),
        {"postgresql_partition_by": "RANGE (at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    token_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    route_template: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_version: Mapped[str] = mapped_column(String(10), nullable=False)
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = ["USAGE_UNITS", "ApiRequestLog", "UsageCounter"]
