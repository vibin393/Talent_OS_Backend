# ruff: noqa: E501
"""Demand insights (Epic 55, stories 55.2-55.5; migrations ``20261008_402_02_field_sources`` and
``20261008_402_03_demand_flags``). Owner: ``demand_insights``.

* ``demand_field_sources`` -- the verbatim source quote of each AI-extracted demand field (shown while the field is
  still in ``demands.ai_extracted_fields``). Not audited (the quote is excluded).
* ``demand_suggestions`` -- skill / new-skill / contract / rate-card suggestions a person accepts or dismisses; agent
  tools never change ``demand_skills`` or create ``TenantSkill`` rows (FR-282).
* ``demand_flags`` -- possible-duplicate and SLA-risk flags. A flag never blocks or merges (FR-283).

Internal only (tenant RLS + internal-only org RLS).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

SUGGESTION_KINDS = ("skill", "new_skill", "contract", "rate_card")
SUGGESTION_STATUSES = ("pending", "accepted", "dismissed", "superseded")
FLAG_KINDS = ("possible_duplicate", "sla_risk")
FLAG_STATUSES = ("open", "linked_duplicate", "distinct", "dismissed", "resolved", "expired")
FLAG_BASES = ("prediction", "pipeline_only", "late")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class DemandFieldSource(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "demand_field_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "demand_id", "field", name="uq_demand_field_sources_field"),
        _tenant_fk("demand_id", "demands", "fk_demand_field_sources_demand_id_demands", "CASCADE"),
        _tenant_fk(
            "intake_message_id",
            "demand_intake_messages",
            "fk_demand_field_sources_intake_message",
            "SET NULL",
        ),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    field: Mapped[str] = mapped_column(String(60), nullable=False)
    source_quote: Mapped[str | None] = mapped_column(String(500), nullable=True)
    intake_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class DemandSuggestion(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "demand_suggestions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("demand_id", "demands", "fk_demand_suggestions_demand_id_demands", "CASCADE"),
        _tenant_fk("decided_by", "users", "fk_demand_suggestions_decided_by_users"),
        CheckConstraint(_in("kind", SUGGESTION_KINDS), name="kind"),
        CheckConstraint(_in("status", SUGGESTION_STATUSES), name="status"),
        Index(
            "uq_demand_suggestions_pending",
            "tenant_id",
            "demand_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_demand_suggestions_demand_id", "demand_id"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'pending'"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)


class DemandFlag(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "demand_flags"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("demand_id", "demands", "fk_demand_flags_demand_id_demands", "CASCADE"),
        _tenant_fk("related_demand_id", "demands", "fk_demand_flags_related_demand_id_demands", "CASCADE"),
        _tenant_fk("resolved_by", "users", "fk_demand_flags_resolved_by_users"),
        CheckConstraint(_in("kind", FLAG_KINDS), name="kind"),
        CheckConstraint(_in("status", FLAG_STATUSES), name="status"),
        CheckConstraint(f"basis IS NULL OR {_in('basis', FLAG_BASES)}", name="basis"),
        CheckConstraint("kind <> 'possible_duplicate' OR related_demand_id IS NOT NULL", name="related"),
        Index(
            "uq_demand_flags_open_dedupe",
            "tenant_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_demand_flags_kind_status", "tenant_id", "kind", "status"),
        Index("ix_demand_flags_related_demand_id", "related_demand_id"),
        Index("ix_demand_flags_demand_id", "demand_id"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    related_demand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    milestone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    basis: Mapped[str | None] = mapped_column(String(20), nullable=True)
    p_late: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    next_steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'open'"))
    raised_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)


__all__ = [
    "FLAG_BASES",
    "FLAG_KINDS",
    "FLAG_STATUSES",
    "SUGGESTION_KINDS",
    "SUGGESTION_STATUSES",
    "DemandFieldSource",
    "DemandFlag",
    "DemandSuggestion",
]
