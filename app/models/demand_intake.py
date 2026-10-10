# ruff: noqa: E501
"""Demand intake (Epic 55, story 55.1; migration ``20261008_402_01_demand_intake``). Owner: ``demand_intake``.

* ``demand_agent_settings`` -- one row per tenant: intake mode, mailbox, fallback owner, thresholds, JD template.
* ``demand_intake_routes`` -- **platform** table (non-RLS, listed in ``core.tenancy``): the platform-address token ->
  tenant (no PII).
* ``demand_intake_sender_domains`` -- admin-verified email domain -> Client mapping (never a public webmail domain).
* ``demand_intake_messages`` -- one received email / upload / paste; raw content is purged after 180 days.

Nothing here is portal-visible (tenant RLS + internal-only org RLS).
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
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

INTAKE_MODES = ("off", "connected_mailbox", "platform_address")
SOURCES = ("platform_address", "connected_mailbox", "upload", "paste")
STATUSES = (
    "received", "blocked", "unmatched", "ambiguous", "matched", "processing", "drafted", "no_roles", "failed",
    "discarded",
)  # fmt: skip
NEEDS_ATTENTION = ("unmatched", "ambiguous", "failed", "blocked", "no_roles")
AUTH_RESULTS = ("pass", "fail", "unknown")
MATCH_METHODS = ("contact_email", "verified_domain", "manual", "forwarded_contact")
RAW_RETENTION_DAYS = 180


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class DemandAgentSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "demand_agent_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        _tenant_fk("mailbox_user_id", "users", "fk_demand_agent_settings_mailbox_user_id_users"),
        _tenant_fk("fallback_owner_id", "users", "fk_demand_agent_settings_fallback_owner_id_users"),
        _tenant_fk("sla_scan_owner_id", "users", "fk_demand_agent_settings_sla_scan_owner_id_users"),
        _tenant_fk("updated_by", "users", "fk_demand_agent_settings_updated_by_users"),
        CheckConstraint(_in("intake_mode", INTAKE_MODES), name="intake_mode"),
        CheckConstraint("roles_per_message_max BETWEEN 1 AND 5", name="roles_max"),
        CheckConstraint("duplicate_threshold BETWEEN 0.50 AND 0.95", name="dup_threshold"),
        CheckConstraint("duplicate_window_days BETWEEN 7 AND 365", name="dup_window"),
        CheckConstraint("sla_risk_probability BETWEEN 0.30 AND 0.95", name="sla_probability"),
        CheckConstraint("sla_digest_max_items BETWEEN 5 AND 100", name="digest_max"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    intake_mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'off'"))
    mailbox_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    mailbox_folder: Mapped[str] = mapped_column(
        String(100), nullable=False, server_default=text("'Talent OS Intake'")
    )
    mailbox_cursor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mailbox_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    fallback_owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    accept_forwarded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    require_sender_auth: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    roles_per_message_max: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    duplicate_threshold: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, server_default=text("0.72")
    )
    duplicate_window_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("90"))
    sla_risk_probability: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, server_default=text("0.60")
    )
    sla_scan_owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sla_digest_max_items: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("25"))
    jd_template_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    ack_contact_on_intake: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    route_token: Mapped[str | None] = mapped_column(String(24), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class DemandIntakeRoute(Base):
    """Platform table: platform-address token -> tenant (no PII). Revoked tokens stay for 404s."""

    __tablename__ = "demand_intake_routes"

    token: Mapped[str] = mapped_column(String(24), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DemandIntakeSenderDomain(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "demand_intake_sender_domains"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "domain", "client_id", name="uq_demand_intake_sender_domains_domain"),
        _tenant_fk("client_id", "clients", "fk_demand_intake_sender_domains_client_id_clients"),
        _tenant_fk("verified_by", "users", "fk_demand_intake_sender_domains_verified_by_users"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    domain: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    verified_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)


class DemandIntakeMessage(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "demand_intake_messages"
    __audit_exclude__ = ("extraction", "body_preview")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("raw_file_id", "stored_files", "fk_demand_intake_messages_raw_file_id_stored_files"),
        _tenant_fk("body_text_file_id", "stored_files", "fk_demand_intake_messages_body_file_stored_files"),
        _tenant_fk("forwarded_by_user_id", "users", "fk_demand_intake_messages_forwarded_by_users"),
        _tenant_fk("matched_client_id", "clients", "fk_demand_intake_messages_matched_client_id_clients"),
        _tenant_fk("matched_contact_id", "contacts", "fk_demand_intake_messages_matched_contact_id_contacts"),
        _tenant_fk("owner_user_id", "users", "fk_demand_intake_messages_owner_user_id_users"),
        CheckConstraint(_in("source", SOURCES), name="source"),
        CheckConstraint(_in("status", STATUSES), name="status"),
        CheckConstraint(f"auth_result IS NULL OR {_in('auth_result', AUTH_RESULTS)}", name="auth_result"),
        CheckConstraint(f"match_method IS NULL OR {_in('match_method', MATCH_METHODS)}", name="match_method"),
        Index(
            "uq_demand_intake_messages_external_ref",
            "tenant_id",
            "source",
            "external_ref",
            unique=True,
            postgresql_where=text("external_ref IS NOT NULL"),
        ),
        Index("ix_demand_intake_messages_status", "tenant_id", "status", text("received_at DESC")),
        Index("ix_demand_intake_messages_matched_client_id", "matched_client_id"),
    )

    source: Mapped[str] = mapped_column(String(20), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    from_address: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    from_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    body_text_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    body_preview: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    auth_result: Mapped[str | None] = mapped_column(String(20), nullable=True)
    forwarded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    match_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    matched_client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    matched_contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'received'"))
    status_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)
    extraction_ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    draft_demand_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    group_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "AUTH_RESULTS",
    "INTAKE_MODES",
    "MATCH_METHODS",
    "NEEDS_ATTENTION",
    "RAW_RETENTION_DAYS",
    "SOURCES",
    "STATUSES",
    "DemandAgentSettings",
    "DemandIntakeMessage",
    "DemandIntakeRoute",
    "DemandIntakeSenderDomain",
]
