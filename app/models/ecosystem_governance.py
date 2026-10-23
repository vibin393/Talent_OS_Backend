# ruff: file-ignore[line-too-long]
"""Epic 79 (Phase 5 Governance) tenant tables (migration ``20261010_513_01_p5_gov``).

* ``access_review_settings`` -- one row per Tenant: cadence, stale window, auto-revoke.
* ``access_reviews`` / ``access_review_items`` -- quarterly access reviews (FR-406).
* ``dsr_downstream_items`` -- the erasure fan-out register for a DSR (FR-410); DPO-only, so no per-row audit.

Every other read in the console is over owner facades / guarded reporting projections -- Epic 79 owns no other tables.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

REVIEW_STATUSES = ("open", "completed", "expired")
ITEM_KINDS = ("installation_grant", "user_authorization", "api_key")
ITEM_DECISIONS = ("pending", "confirmed", "reduced", "revoked", "auto_revoked")
DOWNSTREAM_CHANNELS = ("webhook", "connector", "listed_only")
DOWNSTREAM_STATUSES = ("sent", "confirmed", "failed", "manual_required", "statutory_retained")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AccessReviewSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "access_review_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_access_review_settings_tenant_id"),
        UniqueConstraint("tenant_id", "id", name="uq_access_review_settings_tenant_id_id"),
        CheckConstraint("cadence_months BETWEEN 1 AND 12", name="cadence_months"),
        CheckConstraint("stale_after_days BETWEEN 1 AND 3650", name="stale_after_days"),
    )

    cadence_months: Mapped[int] = mapped_column(SmallInteger, server_default=text("3"), nullable=False)
    stale_after_days: Mapped[int] = mapped_column(SmallInteger, server_default=text("90"), nullable=False)
    auto_revoke_stale: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)
    reviewer_role_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class AccessReview(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "access_reviews"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_access_reviews_tenant_id_id"),
        CheckConstraint(_in("status", REVIEW_STATUSES), name="status"),
    )

    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), server_default=text("'open'"), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    decided_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)


class AccessReviewItem(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "access_review_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "review_id"], ["access_reviews.tenant_id", "access_reviews.id"],
            name="fk_access_review_items_review_id_access_reviews", ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "review_id", "kind", "subject_id", name="uq_access_review_items_review_subject"),
        UniqueConstraint("tenant_id", "id", name="uq_access_review_items_tenant_id_id"),
        CheckConstraint(_in("kind", ITEM_KINDS), name="kind"),
        CheckConstraint(_in("decision", ITEM_DECISIONS), name="decision"),
    )

    review_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    stale: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class DsrDownstreamItem(UUIDPk, TenantMixin, Timestamps, Base):
    """Erasure fan-out register (DPO-only; no per-row audit -- the DSR itself is audited by P1 privacy)."""

    __tablename__ = "dsr_downstream_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_dsr_downstream_items_tenant_id_id"),
        CheckConstraint(_in("channel", DOWNSTREAM_CHANNELS), name="channel"),
        CheckConstraint(_in("status", DOWNSTREAM_STATUSES), name="status"),
    )

    dsr_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    channel: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)


__all__ = [
    "ITEM_DECISIONS",
    "ITEM_KINDS",
    "REVIEW_STATUSES",
    "AccessReview",
    "AccessReviewItem",
    "AccessReviewSettings",
    "DsrDownstreamItem",
]
