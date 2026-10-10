"""Provider connector framework (AD-29, NFR-31; migration 20261002_200_06).

* ``provider_connector_configs``: one active config per (tenant, category, legal entity) -- NULL legal entity =
  tenant default (``NULLS NOT DISTINCT``). Credentials (JSON text) and the webhook secret live in the Secrets
  Vault (owner ``provider_config``, names ``credentials`` / ``webhook_secret``; Epic 66: 500_05 moved them from
  the Fernet columns, 500_06 dropped the columns); write-only through the API and never audited.
* ``connector_jobs``: outbound calls, idempotent by ``<category>:<op>:<subject_kind>:<subject_id>:<version>``.
* ``connector_inbound_events``: verified webhook payloads awaiting processing.
* ``connector_hook_lookups`` (platform, no PII): webhook ``config_id`` -> tenant, written via
  ``platform_session`` like ``secure_link_lookups``.

Tenant-owned tables are internal-only for org RLS.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CONNECTOR_CATEGORIES = (
    "esign", "bgv", "einvoice", "payroll", "accounting", "sms", "whatsapp",
    # Epic 36 (300_01): Phase 3 categories (assessment: Epic 40; dsc, tds_statement, fx: Epic 51).
    "assessment", "dsc", "tds_statement", "fx",
    # Epic 53 (400_01, AD-65): voice (fake / manual adapters only in Phase 4).
    "voice",
    # Epic 66 (500_01, FR-335): job boards, HRMS and speech-to-text (Fake / Manual adapters in the foundation).
    "job_board", "hrms", "stt",
    # Epic 79 (513_01, p5_followups): Epic 72 installs org calendar adapters through the Epic 69 marketplace.
    "calendar",
)  # fmt: skip
# Categories allowing several active providers per (tenant, legal entity) (``slot_key`` = provider key; 500_04).
MULTI_PROVIDER_CATEGORIES = ("job_board",)
CONFIG_STATUSES = ("active", "disabled")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "dead")
INBOUND_STATUSES = ("received", "processed", "failed", "held")  # Epic 66 (500_01): held by the kill switch


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ProviderConnectorConfig(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "provider_connector_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("category", CONNECTOR_CATEGORIES), name="category"),
        CheckConstraint(_in("status", CONFIG_STATUSES), name="status"),
        ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_provider_connector_configs_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        # Epic 66 (500_04): one active provider per (entity, slot); ``slot_key`` = provider key for
        # ``MULTI_PROVIDER_CATEGORIES`` (job boards), else ''.
        Index(
            "uq_provider_connector_configs_active",
            "tenant_id",
            "category",
            "legal_entity_id",
            "slot_key",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("status = 'active'"),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "installation_id"],
            ["app_installations.tenant_id", "app_installations.id"],
            name="fk_provider_connector_configs_installation_id_inst",
            ondelete="RESTRICT",
        ),
    )

    category: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(40), nullable=False)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    # Epic 66 (500_04): the owning Installation (connector installs) and the multi-provider slot.
    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    slot_key: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("''"))


class ConnectorJob(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "connector_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key"),
        CheckConstraint(_in("category", CONNECTOR_CATEGORIES), name="category"),
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts"),
        Index("ix_connector_jobs_status", "status", "next_attempt_at"),
        Index("ix_connector_jobs_subject", "subject_kind", "subject_id"),
        Index("ix_connector_jobs_installation_id_status", "installation_id", "status"),
    )

    category: Mapped[str] = mapped_column(String(20), nullable=False)
    op: Mapped[str] = mapped_column(String(60), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=text("'{}'::jsonb"))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)
    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)  # Epic 66 (500_04)


class ConnectorInboundEvent(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "connector_inbound_events"
    __table_args__ = (
        CheckConstraint(_in("category", CONNECTOR_CATEGORIES), name="category"),
        CheckConstraint(_in("status", INBOUND_STATUSES), name="status"),
        ForeignKeyConstraint(
            ["tenant_id", "config_id"],
            ["provider_connector_configs.tenant_id", "provider_connector_configs.id"],
            name="fk_connector_inbound_events_config_id_configs",
            ondelete="CASCADE",
        ),
        Index("ix_connector_inbound_events_status", "status", "received_at"),
        # Epic 66 (500_04): replay dedup by the provider's event id.
        Index(
            "uq_connector_inbound_events_external_event_id",
            "tenant_id",
            "config_id",
            "external_event_id",
            unique=True,
            postgresql_where=text("external_event_id IS NOT NULL"),
        ),
    )

    config_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'received'"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    external_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)  # Epic 66 (500_04)


class ConnectorHookLookup(Base):
    """Platform table: webhook ``config_id`` -> tenant (no PII)."""

    __tablename__ = "connector_hook_lookups"

    config_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
