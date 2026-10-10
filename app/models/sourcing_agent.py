# ruff: noqa: E501
"""Sourcing & Matching Agent state (Epic 56, FR-285..FR-288; migration ``20261008_403_01_src_shortlists``).
Owner: ``app.services.sourcing_agent`` (the agent package never touches these tables, AD-56).

* ``agent_shortlists`` -- one ranked agent shortlist version per run and Demand (exactly one ``current`` per Demand).
  Scores are display copies of the deterministic P1 ``matches.overall`` (AD-8 / AD-71): the agent never writes them.
* ``agent_shortlist_entries`` -- the ranked people with sources, explanation, per-channel contactability, the
  recruiter's decision and the outreach draft (internal until a person approves the send Proposal).
* ``agent_shortlist_dismissals`` -- "dismissed stays dismissed" across versions.
* ``sourcing_agent_settings`` -- one row per tenant (shortlist size, drafts, channel, searches, proposals).

Every table: tenant RLS + internal-only org RLS.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

SHORTLIST_STATUSES = ("current", "superseded")
TRIGGER_KEYS = ("demand_opened", "requirements_changed", "weekly_refresh", "manual", "workflow")
ENTRY_STATUSES = ("suggested", "added_to_pipeline", "dismissed")
DRAFT_STATUSES = ("none", "proposed", "sent", "failed", "suppressed")
CHANNELS = ("email", "sms", "whatsapp")
SOURCE_KINDS = ("match", "pool", "silver_medalist", "search", "rediscovery")
ENTRY_FLAGS = ("no_ai_consent", "new", "dropped_back", "link_removed")
# P1 matching ``DISMISS_REASONS`` (kept equal; asserted in tests).
DISMISS_REASONS = (
    "not_interested", "skills_gap", "experience", "compensation", "location", "availability", "duplicate", "other",
)  # fmt: skip


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _jsonb(default: str) -> Any:
    return mapped_column(JSONB, nullable=False, server_default=text(f"'{default}'::jsonb"))


class AgentShortlist(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "agent_shortlists"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "demand_id", "version"),
        _tenant_fk("demand_id", "demands", "fk_agent_shortlists_demand_id_demands"),
        _tenant_fk("run_id", "agent_runs", "fk_agent_shortlists_run_id_agent_runs"),
        CheckConstraint(_in("status", SHORTLIST_STATUSES), name="status"),
        CheckConstraint(_in("trigger_key", TRIGGER_KEYS), name="trigger_key"),
        CheckConstraint("top_n BETWEEN 5 AND 100", name="top_n"),
        CheckConstraint("summary IS NULL OR char_length(summary) <= 2000", name="summary_length"),
        Index(
            "uq_agent_shortlists_current",
            "tenant_id",
            "demand_id",
            unique=True,
            postgresql_where=text("status = 'current'"),
        ),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'current'"))
    trigger_key: Mapped[str] = mapped_column(String(40), nullable=False)
    considered_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    excluded_counts: Mapped[dict[str, Any]] = _jsonb("{}")
    suppressed_counts: Mapped[dict[str, Any]] = _jsonb("{}")
    top_n: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentShortlistEntry(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "agent_shortlist_entries"
    __pii_subject__ = "candidate"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "shortlist_id", "candidate_id"),
        UniqueConstraint("tenant_id", "shortlist_id", "rank"),
        _tenant_fk(
            "shortlist_id", "agent_shortlists", "fk_agent_shortlist_entries_shortlist_id", ondelete="CASCADE"
        ),
        _tenant_fk("candidate_id", "candidates", "fk_agent_shortlist_entries_candidate_id"),
        CheckConstraint(_in("status", ENTRY_STATUSES), name="status"),
        CheckConstraint(_in("draft_status", DRAFT_STATUSES), name="draft_status"),
        CheckConstraint("status <> 'dismissed' OR dismiss_reason IS NOT NULL", name="dismiss_reason"),
        CheckConstraint(
            f"dismiss_reason IS NULL OR {_in('dismiss_reason', DISMISS_REASONS)}", name="dismiss_reason_value"
        ),
        CheckConstraint(f"best_channel IS NULL OR {_in('best_channel', CHANNELS)}", name="best_channel"),
        CheckConstraint(f"draft_channel IS NULL OR {_in('draft_channel', CHANNELS)}", name="draft_channel"),
        CheckConstraint("draft_body IS NULL OR char_length(draft_body) <= 4000", name="draft_body_length"),
        CheckConstraint("explanation IS NULL OR char_length(explanation) <= 400", name="explanation_length"),
        Index("ix_agent_shortlist_entries_candidate_id", "candidate_id"),
        Index("ix_agent_shortlist_entries_demand_candidate", "demand_id", "candidate_id"),
        Index("ix_agent_shortlist_entries_candidate_sent", "candidate_id", "outreach_sent_at"),
    )
    __audit_exclude__ = ("draft_body", "draft_subject", "agent_note", "explanation")

    shortlist_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    overall: Mapped[Decimal | None] = mapped_column(Numeric(5, 1), nullable=True)
    match_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sources: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    flags: Mapped[list[Any]] = _jsonb("[]")
    contactability: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    best_channel: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    availability_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'suggested'"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismiss_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    draft_channel: Mapped[str | None] = mapped_column(String(10), nullable=True)
    draft_subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    draft_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'none'"))
    outreach_proposal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    outreach_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentShortlistDismissal(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "agent_shortlist_dismissals"
    __pii_subject__ = "candidate"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "demand_id", "candidate_id"),
        _tenant_fk("demand_id", "demands", "fk_agent_shortlist_dismissals_demand_id"),
        _tenant_fk("candidate_id", "candidates", "fk_agent_shortlist_dismissals_candidate_id"),
        CheckConstraint(_in("reason", DISMISS_REASONS), name="reason"),
        Index("ix_agent_shortlist_dismissals_candidate_id", "candidate_id"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    dismissed_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourcingAgentSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created lazily with the defaults)."""

    __tablename__ = "sourcing_agent_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("top_n BETWEEN 5 AND 100", name="top_n"),
        CheckConstraint("draft_top BETWEEN 0 AND 25", name="draft_top"),
        CheckConstraint("max_searches BETWEEN 0 AND 5", name="max_searches"),
        CheckConstraint(_in("preferred_channel", CHANNELS), name="preferred_channel"),
        _tenant_fk("propose_sequence_id", "sequences", "fk_sourcing_agent_settings_sequence_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    top_n: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("25"))
    draft_top: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("10"))
    preferred_channel: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'email'"))
    include_silver_medalists: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    max_searches: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    propose_pipeline_adds: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    propose_sequence_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


__all__ = [
    "CHANNELS",
    "DISMISS_REASONS",
    "DRAFT_STATUSES",
    "ENTRY_FLAGS",
    "ENTRY_STATUSES",
    "SHORTLIST_STATUSES",
    "SOURCE_KINDS",
    "TRIGGER_KEYS",
    "AgentShortlist",
    "AgentShortlistDismissal",
    "AgentShortlistEntry",
    "SourcingAgentSettings",
]
