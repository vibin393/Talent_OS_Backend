"""Unified-platform inbound data loops (hub ingest).

The ATS (Talent OS) is the data hub; spoke apps push domain signals in over the Public API:

* ``candidate_assessment`` — assessment results from Croar (the ``POST /public/v1/assessments`` loop),
  always linked to a resolved Candidate (and optionally an Application). Never creates a Candidate.
* ``engagement_signal`` — engagement / feedback signals from Feedback IQ (the
  ``POST /public/v1/engagement`` loop). Best-effort matched to a Candidate by email; stored even when
  unmatched (the raw subject refs are kept so a later match is possible).

Both are tenant-owned (forced RLS) and idempotent on ``event_id`` (unique per tenant): a delayed relay
or replay of the same source event updates nothing and returns the existing row.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "CASCADE") -> ForeignKeyConstraint:
    """Composite tenant-safe FK ``(tenant_id, col) -> target(tenant_id, id)`` (FK checks bypass RLS)."""
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class CandidateAssessment(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """An assessment result ingested from Croar, linked to a Candidate (+ optional Application)."""

    __tablename__ = "candidate_assessment"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_candidate_assessment_tenant_id_id"),
        UniqueConstraint("tenant_id", "event_id", name="uq_candidate_assessment_tenant_id_event_id"),
        _tenant_fk("candidate_id", "candidates", "fk_candidate_assessment_candidate_id_candidates"),
        Index("ix_candidate_assessment_candidate_id", "candidate_id"),
    )

    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Simple FK (not composite): ON DELETE SET NULL only nulls this column, never the NOT NULL tenant_id.
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("applications.id", ondelete="SET NULL", name="fk_candidate_assessment_application_id_applications"),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(String(60), nullable=False)
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    max_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class EngagementSignal(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """An engagement / feedback signal ingested from Feedback IQ; best-effort matched to a Candidate."""

    __tablename__ = "engagement_signal"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_engagement_signal_tenant_id_id"),
        UniqueConstraint("tenant_id", "event_id", name="uq_engagement_signal_tenant_id_event_id"),
        Index("ix_engagement_signal_matched_candidate_id", "matched_candidate_id"),
        Index("ix_engagement_signal_subject_email", "subject_email"),
    )

    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Raw subject refs as sent (kept even when unmatched, so a later match is possible).
    subject_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject_ats_candidate_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Simple FK (not composite): ON DELETE SET NULL only nulls this column — the signal is kept even if the
    # matched candidate is later deleted (an engagement signal is stored regardless of a match).
    matched_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("candidates.id", ondelete="SET NULL", name="fk_engagement_signal_matched_candidate_id_candidates"),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(String(60), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    summary: Mapped[str] = mapped_column(String(2000), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
