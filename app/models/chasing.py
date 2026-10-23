# ruff: file-ignore[line-too-long]
"""Compliance chasing and BGV provider follow-ups (Epic 62, FR-308 / FR-309; migration ``20261008_409_03_chases``).
Owner: ``chasing``.

* ``compliance_chases`` (audited) -- one templated reminder per item and cadence offset (UNIQUE
  ``(tenant_id, item_kind, item_ref, offset_days)``), sent by the Compliance Agent on top of the P2 reminders.
* ``bgv_follow_ups`` (audited) -- one status request to a BGV provider about an overdue check (connector poll or a
  templated email to the provider contact on record); ``seq`` counts per check.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

ITEM_KINDS = ("checklist_item", "credential", "permit")
DUE_BASES = ("start_date", "expiry")
FOLLOW_UP_CHANNELS = ("connector", "email")
FOLLOW_UP_STATUSES = ("sent", "failed", "status_received")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class ComplianceChase(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "compliance_chases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id", "item_kind", "item_ref", "offset_days", name="uq_compliance_chases_item_offset"
        ),
        _tfk("candidate_id", "candidates", "fk_compliance_chases_candidate_id"),
        CheckConstraint(_in("item_kind", ITEM_KINDS), name="item_kind"),
        CheckConstraint(_in("due_basis", DUE_BASES), name="due_basis"),
        Index("ix_compliance_chases_candidate_sent", "candidate_id", "sent_at"),
        Index("ix_compliance_chases_placement_id", "placement_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    item_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    item_ref: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    item_label: Mapped[str] = mapped_column(String(200), nullable=False)
    due_basis: Mapped[str] = mapped_column(String(12), nullable=False)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    offset_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), nullable=False, server_default=text("'{}'::varchar[]")
    )
    suppressed: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    message_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BgvFollowUp(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "bgv_follow_ups"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "check_id", "seq", name="uq_bgv_follow_ups_check_seq"),
        _tfk("check_id", "bgv_checks", "fk_bgv_follow_ups_check_id", "CASCADE"),
        _tfk("case_id", "bgv_cases", "fk_bgv_follow_ups_case_id", "CASCADE"),
        CheckConstraint(_in("channel", FOLLOW_UP_CHANNELS), name="channel"),
        CheckConstraint(_in("status", FOLLOW_UP_STATUSES), name="status"),
        Index("ix_bgv_follow_ups_case_id", "case_id"),
    )

    check_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    seq: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'sent'"))
    provider_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["DUE_BASES", "ITEM_KINDS", "BgvFollowUp", "ComplianceChase"]
