# ruff: file-ignore[line-too-long]
"""Governed MCP Server (Epic 73, AD-84, FR-375..FR-380; migration ``20261010_507_01``). Owner: ``app.services.mcp_store``.

Tenant tables (RLS + internal-only org RLS):

* ``mcp_sessions``: one row per MCP session (the session id is stored as its SHA-256 only); the P4 ``agent_runs`` row
  (origin ``external``) holds the tool calls. Not audited (lifecycle audited explicitly).
* ``mcp_access_holds`` (audited): a user's MCP access paused by the bulk-read detector or an administrator.
* ``mcp_settings`` (audited; one row per tenant): limits, allowed browser origins and the first-connection notice.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

SESSION_STATUSES = ("active", "paused", "ended")
END_REASONS = (
    "client_closed", "idle", "kill_switch", "ai_kill_switch", "mcp_disabled", "uninstalled",
    "authorization_revoked", "user_deactivated", "bulk_read", "admin",
)  # fmt: skip
HOLD_REASONS = ("bulk_read", "admin")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


class McpSession(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "mcp_sessions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("session_hash", name="uq_mcp_sessions_session_hash"),
        _tenant_fk("installation_id", "app_installations", "fk_mcp_sessions_installation_id_app_installations"),
        _tenant_fk("user_id", "users", "fk_mcp_sessions_user_id_users"),
        CheckConstraint(_in("status", SESSION_STATUSES), name="status"),
        CheckConstraint(_in("end_reason", END_REASONS), name="end_reason"),
        CheckConstraint("(status = 'ended') = (ended_at IS NOT NULL)", name="ended"),
        Index("ix_mcp_sessions_user_started", "tenant_id", "user_id", text("started_at DESC")),
        Index("ix_mcp_sessions_status", "tenant_id", "status"),
        Index("ix_mcp_sessions_run_id", "run_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_family_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    session_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(12), nullable=False)
    client_name: Mapped[str] = mapped_column(String(120), nullable=False)
    client_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'active'"))
    end_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    proposal_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    refusal_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    violation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    redaction_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    distinct_candidates: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class McpAccessHold(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "mcp_access_holds"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("user_id", "users", "fk_mcp_access_holds_user_id_users"),
        _tenant_fk("session_id", "mcp_sessions", "fk_mcp_access_holds_session_id_mcp_sessions"),
        _tenant_fk("released_by", "users", "fk_mcp_access_holds_released_by_users"),
        CheckConstraint(_in("reason", HOLD_REASONS), name="reason"),
        Index(
            "uq_mcp_access_holds_open", "tenant_id", "user_id", unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(12), nullable=False)
    held_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    auto_release_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class McpSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created on enable or the first settings save)."""

    __tablename__ = "mcp_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("calls_per_minute IS NULL OR calls_per_minute BETWEEN 5 AND 120", name="calls_per_minute"),
        CheckConstraint("calls_per_user_day BETWEEN 100 AND 2000", name="calls_per_user_day"),
        CheckConstraint("proposals_per_hour BETWEEN 1 AND 25", name="proposals_per_hour"),
        CheckConstraint("bulk_read_threshold BETWEEN 50 AND 500", name="bulk_read_threshold"),
        CheckConstraint("max_results BETWEEN 10 AND 50", name="max_results"),
        _tenant_fk("updated_by", "users", "fk_mcp_settings_updated_by_users"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    calls_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calls_per_user_day: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2000"))
    proposals_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("25"))
    bulk_read_threshold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("500"))
    max_results: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("50"))
    allowed_origins: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    notify_user_on_first_connection: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


__all__ = ["END_REASONS", "HOLD_REASONS", "SESSION_STATUSES", "McpAccessHold", "McpSession", "McpSettings"]
