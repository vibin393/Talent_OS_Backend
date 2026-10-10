"""Idempotency keys (AD-23, NFR-25), used by ``app/core/idempotency.py``. Tenant RLS + internal-only org RLS;
rows expire after 24 h (``maintenance.purge_idempotency_keys``). Migration 20261002_200_06."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CHAR, DateTime, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, Timestamps, UUIDPk


class IdempotencyKey(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("tenant_id", "principal_type", "principal_id", "key"),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    principal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response: Mapped[Any | None] = mapped_column(JSONB(), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
