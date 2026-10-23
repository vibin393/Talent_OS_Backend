# ruff: file-ignore[line-too-long]
"""AI campaign copy (Epic 78, FR-401..FR-403, AD-91; migration ``20261010_512_01_copy_drafts``).
Owner: ``app.services.campaign_copy``.

* ``campaign_copy_requests`` -- one request per target (a draft Campaign or a Sequence step): the brief, the
  aggregate segment specs (never person ids), the variants per segment and the ``campaign_copy`` agent run (audited).
* ``campaign_copy_drafts`` -- one row per segment x variant: the generated copy (marketing text with merge-field
  placeholders), deterministic findings, edits and the human approval (★). Lifecycle actions are audited explicitly.

A Sequence step is addressed by ``sequence_id`` + ``step_position`` (P3 rewrites step rows on every edit, so
``sequence_step_id`` is informational only and carries no foreign key). No column holds recipient data.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CHANNELS = ("email", "sms", "whatsapp")
REQUEST_STATUSES = ("requested", "generating", "generated", "failed", "cancelled")
OPEN_REQUEST_STATUSES = ("requested", "generating")
DRAFT_STATUSES = ("generated", "checked", "edited", "approved", "used", "discarded")
MAX_SEGMENTS = 20
MAX_VARIANTS = 3


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class CampaignCopyRequest(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "campaign_copy_requests"
    __audit_exclude__ = ("brief", "segments")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk("campaign_id", "campaigns", "fk_campaign_copy_requests_campaign", "CASCADE"),
        _fk("sequence_id", "sequences", "fk_campaign_copy_requests_sequence", "CASCADE"),
        _fk("requested_by", "users", "fk_campaign_copy_requests_requested_by"),
        CheckConstraint("(campaign_id IS NULL) <> (sequence_id IS NULL)", name="one_target"),
        CheckConstraint("sequence_id IS NULL OR step_position IS NOT NULL", name="step_position"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        CheckConstraint(_in("status", REQUEST_STATUSES), name="status"),
        CheckConstraint(f"variants_per_segment BETWEEN 1 AND {MAX_VARIANTS}", name="variants"),
        Index("ix_campaign_copy_requests_campaign", "tenant_id", "campaign_id", "status"),
        Index("ix_campaign_copy_requests_sequence", "tenant_id", "sequence_id", "step_position"),
        Index("ix_campaign_copy_requests_agent_run_id", "agent_run_id"),
    )

    campaign_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sequence_step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    step_position: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    brief: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    segments: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    variants_per_segment: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    channel_template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'requested'"))
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)


class CampaignCopyDraft(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "campaign_copy_drafts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id", "request_id", "segment_key", "variant_no", name="uq_campaign_copy_drafts_variant"
        ),
        _fk("request_id", "campaign_copy_requests", "fk_campaign_copy_drafts_request", "CASCADE"),
        _fk("approved_by", "users", "fk_campaign_copy_drafts_approved_by"),
        _fk("edited_by", "users", "fk_campaign_copy_drafts_edited_by"),
        _fk("warnings_acknowledged_by", "users", "fk_campaign_copy_drafts_ack_by"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        CheckConstraint(_in("status", DRAFT_STATUSES), name="status"),
        CheckConstraint(f"variant_no BETWEEN 1 AND {MAX_VARIANTS}", name="variant_no"),
        CheckConstraint("body_html IS NULL OR octet_length(body_html) <= 51200", name="body_size"),
        Index("ix_campaign_copy_drafts_campaign", "tenant_id", "campaign_id", "status"),
        Index("ix_campaign_copy_drafts_sequence", "tenant_id", "sequence_id", "step_position", "status"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sequence_step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    step_position: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    segment_key: Mapped[str] = mapped_column(String(40), nullable=False)
    segment_label: Mapped[str] = mapped_column(String(120), nullable=False)
    variant_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    preheader: Mapped[str | None] = mapped_column(String(200), nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    job_link_blocks: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'generated'"))
    findings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    warnings_acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    approved_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    edited_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rubber_stamp: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    used_in_campaign_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "CHANNELS",
    "DRAFT_STATUSES",
    "MAX_SEGMENTS",
    "MAX_VARIANTS",
    "OPEN_REQUEST_STATUSES",
    "REQUEST_STATUSES",
    "CampaignCopyDraft",
    "CampaignCopyRequest",
]
