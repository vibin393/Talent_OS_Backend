# ruff: noqa: E501
"""Connector runtime state (Epic 66, AD-81, FR-335; migration ``20261009_500_03_p5_platform``). Owner:
``app.services.integrations``.

* ``connector_sync_state``: one row per (installation, per-entity config, sync job) -- cursor, schedule, failures and an
  overlap lock (``lock_until``).
* ``connector_links``: the external-id map (local entity <-> provider id), unique both ways per installation; the
  ``candidate_id`` column serves the erasure handler (unlink + null ``external_url``).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, Timestamps, UUIDPk

LINK_STATUSES = ("linked", "pending", "conflict", "error", "unlinked")


def _inst_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "installation_id"],
        ["app_installations.tenant_id", "app_installations.id"],
        name=f"fk_{table}_installation_id_inst",
        ondelete="CASCADE",
    )


class ConnectorSyncState(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "connector_sync_state"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id",
            "installation_id",
            "config_id",
            "job_key",
            name="uq_connector_sync_state_job",
            postgresql_nulls_not_distinct=True,
        ),
        _inst_fk("connector_sync_state"),
        Index("ix_connector_sync_state_next_run_at", "next_run_at", postgresql_where=text("enabled")),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    job_key: Mapped[str] = mapped_column(String(60), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConnectorLink(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "connector_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id", "installation_id", "entity_type", "local_id", name="uq_connector_links_local"
        ),
        UniqueConstraint(
            "tenant_id", "installation_id", "entity_type", "external_id", name="uq_connector_links_external"
        ),
        _inst_fk("connector_links"),
        CheckConstraint(f"status IN ({', '.join(repr(s) for s in LINK_STATUSES)})", name="status"),
        Index("ix_connector_links_candidate_id", "candidate_id"),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    local_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    external_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    local_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    external_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'linked'"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


__all__ = ["LINK_STATUSES", "ConnectorLink", "ConnectorSyncState"]
