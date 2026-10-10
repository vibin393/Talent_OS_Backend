"""Matching tables (Epic 5, stories 5.4/5.5). Tenant-owned, forced RLS (migration 20261001_53).

* ``matches`` -- one row per (demand, candidate). Scores are derived data written by the scoring run with
  Core upserts (not audited); user actions (dismiss / restore / add to pipeline) go through the ORM and are
  audited (``AuditedMixin``; score columns are excluded from the audit diff).
* ``match_runs`` -- one row per scoring run with the rediscovery funnel.
* ``matching_settings`` -- one row per tenant: weights, thresholds, proxy fields, FX table (audited).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

EXPLANATION_STATUSES = ("pending", "ready", "failed", "skipped")
DISMISS_REASONS = (
    "not_interested",
    "skills_gap",
    "experience",
    "compensation",
    "location",
    "availability",
    "duplicate",
    "other",
)
RUN_TRIGGERS = ("opened", "requirements_changed", "manual", "sweep", "backfill")
RUN_STATUSES = ("running", "completed", "failed", "skipped")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(column: str, target: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete="CASCADE"
    )


class Match(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("demand_id", "candidate_id"),
        UniqueConstraint("tenant_id", "id"),
        _fk("demand_id", "demands", "fk_matches_demand_id_demands"),
        _fk("candidate_id", "candidates", "fk_matches_candidate_id_candidates"),
        CheckConstraint(_in("explanation_status", EXPLANATION_STATUSES), name="explanation_status"),
        CheckConstraint(
            f"dismiss_reason IS NULL OR {_in('dismiss_reason', DISMISS_REASONS)}", name="dismiss_reason"
        ),
        Index(
            "ix_matches_tenant_id_demand_id_overall",
            "tenant_id",
            "demand_id",
            text("overall DESC"),
            postgresql_where=text("dismissed_at IS NULL AND NOT is_stale"),
        ),
        Index("ix_matches_tenant_id_candidate_id", "tenant_id", "candidate_id"),
    )
    # Derived columns are rewritten by every scoring run: only user actions belong in the audit log. The free
    # text note may hold personal data and is kept out of it as well.
    __audit_exclude__ = (
        "overall",
        "sub_scores",
        "evidence",
        "missing_mandatory",
        "scoring_version",
        "input_hash",
        "computed_at",
        "explanation",
        "explanation_status",
        "explanation_action_id",
        "explanation_input_hash",
        "is_stale",
        "dismiss_note",
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    overall: Mapped[Decimal] = mapped_column(Numeric(5, 1), nullable=False)
    sub_scores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    missing_mandatory: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    scoring_version: Mapped[str] = mapped_column(String(20), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation_status: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'pending'")
    )
    explanation_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    explanation_input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dismiss_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dismiss_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_to_pipeline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class MatchRun(UUIDPk, TenantMixin, Base):
    __tablename__ = "match_runs"
    __table_args__ = (
        _fk("demand_id", "demands", "fk_match_runs_demand_id_demands"),
        CheckConstraint(_in("trigger", RUN_TRIGGERS), name="trigger"),
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        Index(
            "ix_match_runs_tenant_id_demand_id_started_at", "tenant_id", "demand_id", text("started_at DESC")
        ),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    considered: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    funnel: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class MatchingSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "matching_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("strong_threshold BETWEEN 0 AND 100", name="strong_threshold"),
        CheckConstraint("relevant_threshold BETWEEN 0 AND 100", name="relevant_threshold"),
        CheckConstraint("explain_top_n BETWEEN 0 AND 100", name="explain_top_n"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    weights: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    strong_threshold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("70"))
    relevant_threshold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("40"))
    proxy_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    explain_top_n: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("25"))
    # Units of INR per unit of currency; overrides ``scoring.DEFAULT_FX`` (no tenant FX table exists yet).
    fx_rates: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
