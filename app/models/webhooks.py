# ruff: noqa: E501
"""Webhooks (Epic 68, AD-79/AD-80, FR-349..FR-353; migrations ``20261010_502_01..03``). Owner: ``app.services.webhooks``.

Tenant tables (RLS + internal-only org RLS):

* ``webhook_subscriptions`` (audited; the URL is audited, the signing secret lives in the vault, owner type
  ``webhook_subscription``): owned by the Tenant or by an Installation.
* ``webhook_delivery_keys``: the sink idempotency index (one row per (subscription, event)).
* ``webhook_deliveries``: RANGE-partitioned monthly on ``created_at`` (PK ``(id, created_at)``); the delivery log.
* ``webhook_echo_endpoints`` / ``webhook_echo_receipts``: the sandbox-only built-in receiver.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

OWNER_KINDS = ("tenant", "installation")
SUBSCRIPTION_STATUSES = ("pending_verification", "active", "disabled")
DISABLED_REASONS = ("manual", "gone", "failing", "egress_refused", "uninstalled", "scope_lost")
DELIVERY_KINDS = ("event", "replay", "test", "verification")
DELIVERY_STATUSES = ("pending", "in_flight", "retrying", "succeeded", "failed", "held", "cancelled")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class WebhookSubscription(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "webhook_subscriptions"
    __audit_exclude__ = (
        "consecutive_failures", "failure_streak_started_at", "streak_notified_at", "last_success_at",
        "last_failure_at", "verification_attempts",
    )  # fmt: skip
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "installation_id"], ["app_installations.tenant_id", "app_installations.id"],
            name="fk_webhook_subscriptions_installation_id_app_installations", ondelete="RESTRICT",
        ),
        CheckConstraint(_in("owner_kind", OWNER_KINDS), name="owner_kind"),
        CheckConstraint(_in("status", SUBSCRIPTION_STATUSES), name="status"),
        CheckConstraint(f"disabled_reason IS NULL OR {_in('disabled_reason', DISABLED_REASONS)}", name="disabled_reason"),
        CheckConstraint("(owner_kind = 'installation') = (installation_id IS NOT NULL)", name="owner"),
        CheckConstraint("all_events OR cardinality(event_types) > 0", name="types"),
        CheckConstraint("(status = 'disabled') = (disabled_reason IS NOT NULL)", name="disabled"),
        CheckConstraint("max_in_flight BETWEEN 1 AND 5", name="max_in_flight"),
        Index("ix_webhook_subscriptions_tenant_id_status", "tenant_id", "status"),
        Index("ix_webhook_subscriptions_tenant_id_installation_id", "tenant_id", "installation_id"),
        Index("ix_webhook_subscriptions_url_host", "url_host"),
    )

    owner_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_host: Mapped[str] = mapped_column(String(253), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(String(60)), nullable=False, server_default=text("'{}'"))
    all_events: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'pending_verification'"))
    disabled_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    verification_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    max_in_flight: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("5"))
    suppress_own_changes: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failure_streak_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    streak_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    secret_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by_installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    notify_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookDeliveryKey(TenantMixin, Base):
    __tablename__ = "webhook_delivery_keys"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "subscription_id", "event_id"),)

    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    delivery_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class WebhookDelivery(TenantMixin, Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at"),
        CheckConstraint(_in("kind", DELIVERY_KINDS), name="kind"),
        CheckConstraint(_in("status", DELIVERY_STATUSES), name="status"),
        CheckConstraint("status NOT IN ('pending', 'retrying') OR next_attempt_at IS NOT NULL", name="retry"),
        Index("ix_webhook_deliveries_sub_created", "tenant_id", "subscription_id", text("created_at DESC")),
        Index(
            "ix_webhook_deliveries_due", "status", "next_attempt_at",
            postgresql_where=text("status IN ('pending', 'retrying')"),
        ),
        Index("ix_webhook_deliveries_tenant_id_event_id", "tenant_id", "event_id"),
        Index("ix_webhook_deliveries_candidate_id", "candidate_id"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    event_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    subject_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'event'"))
    replay_of_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    body: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    last_status_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(60), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class WebhookEchoEndpoint(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "webhook_echo_endpoints"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), UniqueConstraint("token_hash"))

    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class WebhookEchoReceipt(UUIDPk, TenantMixin, Base):
    __tablename__ = "webhook_echo_receipts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index("ix_webhook_echo_receipts_token_received", "tenant_id", "echo_token_hash", text("received_at DESC")),
    )

    echo_token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)


__all__ = [
    "DELIVERY_KINDS",
    "DELIVERY_STATUSES",
    "DISABLED_REASONS",
    "OWNER_KINDS",
    "SUBSCRIPTION_STATUSES",
    "WebhookDelivery",
    "WebhookDeliveryKey",
    "WebhookEchoEndpoint",
    "WebhookEchoReceipt",
    "WebhookSubscription",
]
