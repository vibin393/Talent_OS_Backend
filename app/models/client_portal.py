"""Client Portal content tables (Epic 18, FR-100–FR-103). All tenant-owned with forced RLS and org RLS on
``client_id`` (migrations ``20261005_205_02..04``).

* ``interview_slot_proposals`` -- client-proposed / recruiter-offered interview slots (nothing is scheduled until
  the recruiter runs the P1 scheduler from the Task).
* ``client_user_delegations`` -- out-of-office delegates of a Client User (FR-102).
* ``client_compliance_packs`` / ``client_compliance_pack_items`` -- monthly compliance evidence published to the
  client; ``client_portal_downloads`` logs every item download.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

SLOT_KINDS = ("client_proposed", "recruiter_offered")
SLOT_STATUSES = ("open", "chosen", "scheduled", "declined", "expired")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_{table}_{column}_{target}"[:63],
        ondelete=ondelete,
    )


class InterviewSlotProposal(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "interview_slot_proposals"
    __table_args__ = (
        _tfk("interview_slot_proposals", "client_id", "clients"),
        _tfk("interview_slot_proposals", "application_id", "applications", "CASCADE"),
        _tfk("interview_slot_proposals", "demand_id", "demands"),
        CheckConstraint(_in("kind", SLOT_KINDS), name="kind"),
        CheckConstraint(_in("status", SLOT_STATUSES), name="status"),
        Index("ix_interview_slot_proposals_application_id", "application_id"),
        Index("ix_interview_slot_proposals_client_id", "client_id", "status"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submission_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    slots: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    chosen_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="open")
    created_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    interview_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ClientUserDelegation(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "client_user_delegations"
    __table_args__ = (
        _tfk("client_user_delegations", "client_id", "clients"),
        _tfk("client_user_delegations", "client_user_id", "client_users", "CASCADE"),
        _tfk("client_user_delegations", "delegate_client_user_id", "client_users", "CASCADE"),
        CheckConstraint("ends_on >= starts_on", name="dates"),
        CheckConstraint("delegate_client_user_id <> client_user_id", name="not_self"),
        Index("ix_client_user_delegations_user", "client_user_id"),
        Index("ix_client_user_delegations_delegate", "delegate_client_user_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    delegate_client_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClientCompliancePack(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "client_compliance_packs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tfk("client_compliance_packs", "client_id", "clients"),
        Index("ix_client_compliance_packs_client_id", "client_id", "period_month"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClientCompliancePackItem(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "client_compliance_pack_items"
    __table_args__ = (
        _tfk("client_compliance_pack_items", "pack_id", "client_compliance_packs", "CASCADE"),
        CheckConstraint("file_id IS NOT NULL OR document_id IS NOT NULL", name="source"),
        Index("ix_client_compliance_pack_items_pack_id", "pack_id"),
    )

    pack_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)  # inherited
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)


class ClientPortalDownload(UUIDPk, TenantMixin, Base):
    __tablename__ = "client_portal_downloads"
    __table_args__ = (
        _tfk("client_portal_downloads", "client_id", "clients"),
        Index("ix_client_portal_downloads_client_id", "client_id", "at"),
        Index("ix_client_portal_downloads_item_id", "item_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
