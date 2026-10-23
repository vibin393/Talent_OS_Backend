# ruff: file-ignore[line-too-long]
"""Transactional outbox (Epic 66, AD-79, FR-334; migration ``20261009_500_03_p5_platform``). Owner:
``app.services.public_events``.

``outbox_events`` (tenant, not audited): one row per mapped domain event, written in the domain transaction by the
``core.events.subscribe_in_tx`` writer; ``seq`` orders rows (increasing per subject, gaps allowed). The relay
(``public_events.dispatch``) hands pending rows to the registered sinks and marks them dispatched. Retention class
``public_events`` (30 days).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, Timestamps, UUIDPk

VISIBILITIES = ("public", "internal")
DISPATCH_STATUSES = ("pending", "dispatched")


class OutboxEvent(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("seq"),
        CheckConstraint("visibility IN ('public', 'internal')", name="visibility"),
        CheckConstraint("dispatch_status IN ('pending', 'dispatched')", name="dispatch_status"),
        Index("ix_outbox_events_pending", "tenant_id", "dispatch_status", "seq"),
        Index("ix_outbox_events_type", "tenant_id", "event_type", "occurred_at"),
        Index("ix_outbox_events_subject", "subject_type", "subject_id", "seq"),
        Index("ix_outbox_events_occurred_at", "occurred_at"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    event_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    visibility: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_event: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    app_installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dispatch_status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'pending'"))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["DISPATCH_STATUSES", "VISIBILITIES", "OutboxEvent"]
