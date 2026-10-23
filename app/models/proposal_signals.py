# ruff: file-ignore[line-too-long]
"""Proposal review signals (Epic 65, story 65.2, SM-C14; branch ``p4_governance``, band 412).

``proposal_review_signals`` -- how a person reviewed an agent Proposal: first opened, open count, decision, the time
from first open to decision and the ``rubber_stamp`` flag (an approval of a never-opened Proposal, or within 5 s of
opening it). Staff ids and timings only -- no Proposal content. Telemetry: not audited.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, Timestamps, UUIDPk

REVIEW_DECISIONS = ("approved", "edited_approved", "rejected")
RUBBER_STAMP_MS = 5000


class ProposalReviewSignal(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "proposal_review_signals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "proposal_id", "user_id"),
        ForeignKeyConstraint(
            ["tenant_id", "proposal_id"],
            ["agent_proposals.tenant_id", "agent_proposals.id"],
            name="fk_proposal_review_signals_proposal_id_agent_proposals",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_proposal_review_signals_user_id_users",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approved', 'edited_approved', 'rejected')", name="decision"
        ),
        Index("ix_proposal_review_signals_decided", "tenant_id", "decided_at"),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    first_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    open_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decision_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rubber_stamp: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


__all__ = ["REVIEW_DECISIONS", "RUBBER_STAMP_MS", "ProposalReviewSignal"]
