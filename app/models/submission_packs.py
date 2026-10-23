# ruff: file-ignore[line-too-long]
"""Submission packs (Epic 61, FR-305..FR-307; migration ``20261008_408_01_sub_packs``). Owner:
``app.services.submission_packs`` (the agent package never touches these tables, AD-56).

* ``client_submission_rules`` -- one optional row per Client (absent = defaults): resume format, contact masking,
  write-up length and guidance, duplicate window override, candidate-consent requirement, notice period.
* ``submission_packs`` -- a prepared, internal draft of a client submission (resume choice, write-up, suggested rates,
  pre-send checks). It never becomes a Submission by itself: a person opens it in the P1 Submission dialog and sends.
  One ``open`` pack per Application; ``used`` packs record the Submission and what the person changed.

Every table: tenant RLS + internal-only org RLS (nothing here is Client-visible in Phase 4).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
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

RESUME_FORMATS = ("either", "original", "client_formatted")
PACK_STATUSES = ("draft", "ready", "blocked")
LIFECYCLES = ("open", "used", "discarded", "stale")
RESUME_KINDS = ("original", "client_formatted")
WRITE_UP_STATUSES = ("pending", "drafted", "failed", "edited")
RATE_UNITS = ("hour", "day", "month", "year")  # P1 ``pipeline.RATE_UNITS``
PREPARED_BY = ("agent", "user", "system")
MAX_SUMMARY = 3000


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class ClientSubmissionRules(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "client_submission_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "client_id"),
        _tenant_fk("client_id", "clients", "fk_client_submission_rules_client_id"),
        CheckConstraint(_in("resume_format", RESUME_FORMATS), name="resume_format"),
        CheckConstraint("write_up_max_words BETWEEN 40 AND 400", name="words"),
        CheckConstraint(
            "duplicate_window_days IS NULL OR duplicate_window_days BETWEEN 1 AND 730", name="dup_days"
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    resume_format: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'either'"))
    mask_contact_details: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    write_up_max_words: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("150"))
    write_up_instructions: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    duplicate_window_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    require_candidate_consent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    show_notice_period: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class SubmissionPack(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "submission_packs"
    __pii_subject__ = "candidate"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        _tenant_fk("application_id", "applications", "fk_submission_packs_application_id"),
        _tenant_fk("candidate_id", "candidates", "fk_submission_packs_candidate_id"),
        _tenant_fk("demand_id", "demands", "fk_submission_packs_demand_id"),
        _tenant_fk("client_id", "clients", "fk_submission_packs_client_id"),
        _tenant_fk("submission_id", "submissions", "fk_submission_packs_submission_id"),
        CheckConstraint(_in("status", PACK_STATUSES), name="status"),
        CheckConstraint(_in("lifecycle", LIFECYCLES), name="lifecycle"),
        CheckConstraint(f"resume_kind IS NULL OR {_in('resume_kind', RESUME_KINDS)}", name="resume_kind"),
        CheckConstraint(_in("write_up_status", WRITE_UP_STATUSES), name="write_up_status"),
        CheckConstraint(f"pay_rate_unit IS NULL OR {_in('pay_rate_unit', RATE_UNITS)}", name="pay_unit"),
        CheckConstraint(f"bill_rate_unit IS NULL OR {_in('bill_rate_unit', RATE_UNITS)}", name="bill_unit"),
        CheckConstraint(_in("prepared_by_kind", PREPARED_BY), name="prepared_by"),
        CheckConstraint(
            f"summary_text IS NULL OR char_length(summary_text) <= {MAX_SUMMARY}", name="summary_len"
        ),
        CheckConstraint("lifecycle <> 'used' OR submission_id IS NOT NULL", name="used"),
        Index(
            "uq_submission_packs_open",
            "tenant_id",
            "application_id",
            unique=True,
            postgresql_where=text("lifecycle = 'open'"),
        ),
        Index("ix_submission_packs_tenant_status", "tenant_id", "status"),
        Index("ix_submission_packs_client_id", "client_id"),
        Index("ix_submission_packs_submission_id", "submission_id"),
        Index("ix_submission_packs_candidate_id", "candidate_id"),
        Index("ix_submission_packs_application_id", "application_id"),
    )
    __audit_exclude__ = ("summary_text", "checks_detail", "user_changes")

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'draft'"))
    lifecycle: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'open'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    resume_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resume_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resume_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    document_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    write_up_status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'pending'"))
    pay_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    bill_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    pay_rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    bill_rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    email_subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    checks_detail: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    missing: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, server_default=text("'{}'::varchar[]")
    )
    blocker_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    warning_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_request_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prepared_by_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    prepared_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    opened_in_dialog_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submission_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    discarded_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


__all__ = [
    "LIFECYCLES",
    "MAX_SUMMARY",
    "PACK_STATUSES",
    "RESUME_FORMATS",
    "RESUME_KINDS",
    "WRITE_UP_STATUSES",
    "ClientSubmissionRules",
    "SubmissionPack",
]
