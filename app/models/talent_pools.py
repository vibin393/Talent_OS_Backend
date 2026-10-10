# ruff: noqa: E501
"""Talent pools and segments (Epic 36 hub table, AD-39; owner Epic 38, branch ``p3_pools``).

``talent_pools`` is created by the foundation (migration ``20261007_300_02_p3_hubs``); Epic 38 adds the FK to
``segments`` and its own columns in ``20261006_302_02_pools``. Internal only: the org policy (no columns) hides
it from client / vendor / worker sessions.

* ``segments`` / ``segment_versions``: saved, versioned semantic definitions (``core.semantic``, AD-42).
* ``talent_pool_members``: materialised membership with a stored reason; removed rows are kept (``removed_at``)
  so a refresh diff can be read back through ``pools_service.member_ids(change=..., refresh_id=...)``.
* ``talent_pool_shares``: carries the P1 saved-list shares over (decision 11).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

POOL_KINDS = ("static", "dynamic")
POOL_VISIBILITIES = ("private", "team", "tenant")
POOL_STATUSES = ("active", "archived")
SEGMENT_SOURCES = ("candidates.candidate", "placements.worker")
MEMBER_SOURCES = ("manual", "segment", "legacy_list", "workflow")
REFRESH_TRIGGERS = ("nightly", "manual", "skill_graph", "created")
REFRESH_STATUSES = ("running", "done", "failed", "skipped_needs_repair")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class TalentPoolSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created lazily with the defaults)."""

    __tablename__ = "talent_pool_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("max_dynamic_pools BETWEEN 1 AND 100000", name="max_dynamic"),
        CheckConstraint("nightly_refresh_local_time ~ '^[0-2][0-9]:[0-5][0-9]$'", name="nightly_time"),
        CheckConstraint("digest_local_time ~ '^[0-2][0-9]:[0-5][0-9]$'", name="digest_time"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    nightly_refresh_local_time: Mapped[str] = mapped_column(
        String(5), nullable=False, server_default=text("'02:00'")
    )
    digest_local_time: Mapped[str] = mapped_column(String(5), nullable=False, server_default=text("'08:00'"))
    max_dynamic_pools: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2000"))
    last_nightly_on: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_digest_on: Mapped[str | None] = mapped_column(String(10), nullable=True)


class Segment(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "segments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("owner_id", "users", "fk_segments_owner_id_users"),
        _tenant_fk("team_id", "teams", "fk_segments_team_id_teams"),
        CheckConstraint(_in("visibility", POOL_VISIBILITIES), name="visibility"),
        CheckConstraint("visibility <> 'team' OR team_id IS NOT NULL", name="team_visibility"),
        CheckConstraint("definition_version >= 1", name="version"),
        Index("ix_segments_owner_id", "owner_id"),
    )
    __audit_exclude__ = ("needs_repair", "repair_issues")

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_key: Mapped[str] = mapped_column(String(60), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    definition_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'private'"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    needs_repair: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    repair_issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SegmentVersion(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """Append-only history of segment definitions."""

    __tablename__ = "segment_versions"
    __table_args__ = (
        _tenant_fk("segment_id", "segments", "fk_segment_versions_segment_id_segments", "CASCADE"),
        UniqueConstraint("segment_id", "version"),
    )

    segment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class TalentPool(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "talent_pools"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("owner_id", "users", "fk_talent_pools_owner_id_users"),
        _tenant_fk("segment_id", "segments", "fk_talent_pools_segment_id_segments"),
        _tenant_fk("team_id", "teams", "fk_talent_pools_team_id_teams"),
        _tenant_fk("legacy_list_id", "candidate_lists", "fk_talent_pools_legacy_list"),
        CheckConstraint(_in("kind", POOL_KINDS), name="kind"),
        CheckConstraint(_in("visibility", POOL_VISIBILITIES), name="visibility"),
        CheckConstraint(_in("status", POOL_STATUSES), name="status"),
        CheckConstraint("kind = 'dynamic' OR segment_id IS NULL", name="segment_dynamic"),
        CheckConstraint("kind = 'static' OR segment_id IS NOT NULL", name="dynamic_segment"),
        CheckConstraint("visibility <> 'team' OR team_id IS NOT NULL", name="team_visibility"),
        Index("ix_talent_pools_owner_id", "owner_id"),
        Index("ix_talent_pools_segment_id", "segment_id"),
        Index(
            "uq_talent_pools_legacy_list_id",
            "legacy_list_id",
            unique=True,
            postgresql_where=text("legacy_list_id IS NOT NULL"),
        ),
        Index(
            "ix_talent_pools_refresh_due_at",
            "refresh_due_at",
            postgresql_where=text("refresh_due_at IS NOT NULL"),
        ),
    )
    __audit_exclude__ = ("member_count", "last_refreshed_at", "refresh_due_at", "last_diff")

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    segment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'private'"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 38 (302_02)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    template_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    legacy_list_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class TalentPoolShare(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "talent_pool_shares"
    __table_args__ = (
        _tenant_fk("pool_id", "talent_pools", "fk_talent_pool_shares_pool_id", "CASCADE"),
        _tenant_fk("user_id", "users", "fk_talent_pool_shares_user_id_users", "CASCADE"),
        _tenant_fk("team_id", "teams", "fk_talent_pool_shares_team_id_teams", "CASCADE"),
        CheckConstraint("(user_id IS NULL) <> (team_id IS NULL)", name="exactly_one_target"),
        UniqueConstraint("pool_id", "user_id"),
        UniqueConstraint("pool_id", "team_id"),
    )

    pool_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class TalentPoolMember(UUIDPk, TenantMixin, Base):
    """Membership rows (not audited per row: each refresh is audited once)."""

    __tablename__ = "talent_pool_members"
    __table_args__ = (
        _tenant_fk("pool_id", "talent_pools", "fk_talent_pool_members_pool_id", "CASCADE"),
        _tenant_fk("candidate_id", "candidates", "fk_talent_pool_members_candidate_id", "CASCADE"),
        CheckConstraint(_in("source", MEMBER_SOURCES), name="source"),
        Index(
            "uq_talent_pool_members_active",
            "pool_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
        Index("ix_talent_pool_members_candidate_id", "candidate_id"),
        Index("ix_talent_pool_members_added_refresh", "pool_id", "added_refresh_id"),
        Index("ix_talent_pool_members_removed_refresh", "pool_id", "removed_refresh_id"),
    )

    pool_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    added_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    added_refresh_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_refresh_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class TalentPoolRefresh(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "talent_pool_refreshes"
    __table_args__ = (
        _tenant_fk("pool_id", "talent_pools", "fk_talent_pool_refreshes_pool_id", "CASCADE"),
        CheckConstraint(_in("trigger", REFRESH_TRIGGERS), name="trigger"),
        CheckConstraint(_in("status", REFRESH_STATUSES), name="status"),
        Index("ix_talent_pool_refreshes_pool_id", "pool_id", "started_at"),
    )
    __audit_exclude__ = ("added", "removed", "evaluated", "duration_ms", "finished_at")

    pool_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'running'"))
    added: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    removed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    evaluated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TalentPoolSubscription(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "talent_pool_subscriptions"
    __table_args__ = (
        _tenant_fk("pool_id", "talent_pools", "fk_talent_pool_subscriptions_pool_id", "CASCADE"),
        _tenant_fk("user_id", "users", "fk_talent_pool_subscriptions_user_id", "CASCADE"),
        UniqueConstraint("pool_id", "user_id"),
        Index("ix_talent_pool_subscriptions_user_id", "user_id"),
    )
    __audit_exclude__ = ("last_digest_at",)

    pool_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DemandPoolPin(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "demand_pool_pins"
    __table_args__ = (
        _tenant_fk("demand_id", "demands", "fk_demand_pool_pins_demand_id", "CASCADE"),
        _tenant_fk("pool_id", "talent_pools", "fk_demand_pool_pins_pool_id", "CASCADE"),
        _tenant_fk("pinned_by", "users", "fk_demand_pool_pins_pinned_by"),
        UniqueConstraint("demand_id", "pool_id"),
        Index("ix_demand_pool_pins_pool_id", "pool_id"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pool_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pinned_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pinned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


__all__ = [
    "MEMBER_SOURCES",
    "POOL_KINDS",
    "POOL_STATUSES",
    "POOL_VISIBILITIES",
    "REFRESH_STATUSES",
    "REFRESH_TRIGGERS",
    "SEGMENT_SOURCES",
    "DemandPoolPin",
    "Segment",
    "SegmentVersion",
    "TalentPool",
    "TalentPoolMember",
    "TalentPoolRefresh",
    "TalentPoolSettings",
    "TalentPoolShare",
    "TalentPoolSubscription",
]
