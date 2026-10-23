# ruff: file-ignore[line-too-long]
"""Tenant Recording Policy (Epic 53 table + gate, FR-294; migration ``20261008_400_03_p4_hubs``). Epic 58 owns the
settings UI / API. Seed per tenant: ``IN`` / NULL / ``candidate_consent``; any jurisdiction not listed, or an unknown
location, is not permitted."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

RECORDING_RULES = ("not_permitted", "candidate_consent", "all_party_consent")


class RecordingPolicy(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "recording_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "updated_by"],
            ["users.tenant_id", "users.id"],
            name="fk_recording_policies_updated_by_users",
            ondelete="RESTRICT",
        ),
        CheckConstraint("rule IN ('not_permitted', 'candidate_consent', 'all_party_consent')", name="rule"),
        Index(
            "uq_recording_policies_jurisdiction",
            "tenant_id",
            "country",
            text("COALESCE(region, '')"),
            unique=True,
        ),
    )

    country: Mapped[str] = mapped_column(String(2), nullable=False)
    region: Mapped[str | None] = mapped_column(String(10), nullable=True)
    rule: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


__all__ = ["RECORDING_RULES", "RecordingPolicy"]
