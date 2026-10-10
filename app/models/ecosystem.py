# ruff: noqa: E501
"""Ecosystem controls and exposure tracking (Epic 66, AD-85/AD-86, D-11; migrations ``500_02`` / ``500_03``).

* ``ecosystem_settings`` (tenant, one row per tenant, lazily created, audited): the tenant Ecosystem Kill Switch, the
  platform operator's per-tenant suspension, MCP enablement storage (73 builds the UI), the app policy and an operator
  Rate Tier override.
* ``platform_ecosystem_controls`` (platform, one row ``id=1``): the global ecosystem suspension.
* ``subject_exposures`` (tenant, not audited): which Installation received which candidate / worker through which
  channel -- written through ``exposure_service.record`` and read by the erasure fan-out (``subject.erased``).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

DEFAULT_APP_POLICY: dict[str, Any] = {
    "publisher_kinds": ["platform", "partner"],
    "private_apps": True,
    "user_self_authorize": True,
    "sensitive_approval_chain_id": None,
}
EXPOSURE_CHANNELS = ("api", "webhook", "connector", "mcp")
_APP_POLICY_SQL = (
    """'{"publisher_kinds": ["platform", "partner"], "private_apps": true, "user_self_authorize": true, """
    """"sensitive_approval_chain_id": null}'::jsonb"""
)


class EcosystemSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "ecosystem_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "kill_switch_by"],
            ["users.tenant_id", "users.id"],
            name="fk_ecosystem_settings_kill_switch_by_users",
            ondelete="RESTRICT",
        ),
    )

    kill_switch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    kill_switch_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kill_switch_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    platform_suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    platform_suspended_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    platform_suspended_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mcp_notice_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    mcp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mcp_enabled_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    mcp_tool_groups: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, server_default=text("'{}'")
    )
    app_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text(_APP_POLICY_SQL)
    )
    rate_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)


class PlatformEcosystemControls(Base):
    """Platform singleton (``id = 1``): the global ecosystem suspension (SELECT / UPDATE for ``talent_app``)."""

    __tablename__ = "platform_ecosystem_controls"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    global_suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    global_suspended_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    global_suspended_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class SubjectExposure(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "subject_exposures"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        # D-25(d): an Installation OR an API key received the subject (exactly one).
        UniqueConstraint(
            "tenant_id",
            "installation_id",
            "api_key_id",
            "subject_type",
            "subject_id",
            "channel",
            name="uq_subject_exposures_subject_channel",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("(installation_id IS NULL) <> (api_key_id IS NULL)", name="recipient"),
        Index("ix_subject_exposures_api_key_id", "api_key_id"),
        ForeignKeyConstraint(
            ["tenant_id", "api_key_id"],
            ["api_keys.tenant_id", "api_keys.id"],
            name="fk_subject_exposures_api_key_id_api_keys",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "installation_id"],
            ["app_installations.tenant_id", "app_installations.id"],
            name="fk_subject_exposures_installation_id_inst",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"channel IN ({', '.join(repr(c) for c in EXPOSURE_CHANNELS)})", name="channel"),
        Index("ix_subject_exposures_subject", "subject_type", "subject_id"),
    )

    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "DEFAULT_APP_POLICY",
    "EXPOSURE_CHANNELS",
    "EcosystemSettings",
    "PlatformEcosystemControls",
    "SubjectExposure",
]
