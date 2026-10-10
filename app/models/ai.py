"""AI gateway tables (AD-6): one ``ai_actions`` row per gateway call, per-tenant AI settings.

Tenant-owned, RLS (migration 20261001_33).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

AI_FEATURES: tuple[str, ...] = (
    "demand_intake",
    "jd_generation",
    "resume_parsing",
    "nl_search",
    "matching",
    "ai_screening",
)
# Epic 27: optional AI receipt read (suggestions only, AD-6).
AI_FEATURES += ("expense_receipt_read",)
# Epic 35 (FR-182): AI commercial-document extraction, off unless the tenant opts in.
AI_FEATURES += ("document_extraction",)
AI_FEATURES_DEFAULT_OFF: frozenset[str] = frozenset({"document_extraction"})
# Epic 37 (FR-189): skill inference from resumes -> reviewable suggestions, off unless the tenant opts in.
AI_FEATURES += ("skill_inference",)
AI_FEATURES_DEFAULT_OFF = AI_FEATURES_DEFAULT_OFF | {"skill_inference"}
# Epic 40 (FR-205): advisory review suggestions for free-form assessment answers, off unless the tenant opts in.
AI_FEATURES += ("assessment_review",)
AI_FEATURES_DEFAULT_OFF = AI_FEATURES_DEFAULT_OFF | {"assessment_review"}
# Epic 48 (FR-244): AI explanation of a prediction's labelled contributions (never the score), opt-in.
AI_FEATURES += ("prediction_explanation",)
AI_FEATURES_DEFAULT_OFF = AI_FEATURES_DEFAULT_OFF | {"prediction_explanation"}
# Epic 53 (FR-264, FR-272): the agentic master switch and one toggle per shipped agent -- all off until the tenant
# acknowledges the enablement notice and turns them on.
AGENT_KEYS: tuple[str, ...] = (
    "copilot", "demand", "sourcing", "screening", "voice_screening", "scheduling", "interview_assist", "submission",
    "compliance", "bgv", "onboarding", "timesheet", "billing", "collections", "analytics",
)  # fmt: skip
AI_FEATURES += ("agentic", *(f"agent_{k}" for k in AGENT_KEYS))
AI_FEATURES_DEFAULT_OFF = AI_FEATURES_DEFAULT_OFF | {"agentic"} | {f"agent_{k}" for k in AGENT_KEYS}
# Epic 66 (§0.3, FR-396 / FR-401): MCP sessions, portal Copilots (77), campaign copy (78) -- off until enabled.
P5_AGENT_KEYS: tuple[str, ...] = (
    "mcp", "copilot_client", "copilot_vendor", "copilot_candidate", "campaign_copy",
)  # fmt: skip
AGENT_KEYS += P5_AGENT_KEYS
AI_FEATURES += tuple(f"agent_{k}" for k in P5_AGENT_KEYS)
AI_FEATURES_DEFAULT_OFF = AI_FEATURES_DEFAULT_OFF | {f"agent_{k}" for k in P5_AGENT_KEYS}
AI_ACTION_STATUSES = ("ok", "failed", "skipped")
AI_ACTION_KINDS = ("single", "agent_step")
AI_DECISIONS = ("accepted", "edited", "rejected")
AI_PROCESSING_BASES = ("consent_required", "notice_based")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AIAction(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "ai_actions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", AI_ACTION_STATUSES), name="status"),
        CheckConstraint(f"decision IS NULL OR {_in('decision', AI_DECISIONS)}", name="decision"),
        Index("ix_ai_actions_subject", "subject_type", "subject_id"),
        Index("ix_ai_actions_tenant_id_created_at", "tenant_id", "created_at"),
        # AI action log filters (Epic 12, migration 20261001_122).
        Index("ix_ai_actions_feature_created_at", "feature", "created_at"),
        Index("ix_ai_actions_initiator_id", "initiator_id"),
        # Epic 53 (400_04): agent loop steps.
        CheckConstraint(_in("kind", AI_ACTION_KINDS), name="kind"),
        Index("ix_ai_actions_agent_run_id", "agent_run_id"),
        Index("ix_ai_actions_agent_key_created_at", "agent_key", "created_at"),
    )
    # Model output can hold personal data (parsed resumes): kept out of the audit log.
    __audit_exclude__ = ("output", "limitations_note")

    feature: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_id: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    initiator_type: Mapped[str] = mapped_column(String(20), nullable=False)
    initiator_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    subject_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    input_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    input_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    limitations_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Epic 35 (NFR-24): ``{"redactions": {kind: n}}`` from the sensitive-input guard (migration 222_04).
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    consent_basis: Mapped[str | None] = mapped_column(String(50), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 53 (AD-57, 400_04): one row per agent loop step; ``agent_run_id`` has no FK (log table).
    kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'single'"))
    agent_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    step_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_tier: Mapped[str | None] = mapped_column(String(10), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AITenantSettings(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "ai_tenant_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id"),
        CheckConstraint(_in("ai_processing_basis", AI_PROCESSING_BASES), name="ai_processing_basis"),
        CheckConstraint("monthly_token_budget IS NULL OR monthly_token_budget >= 0", name="budget"),
    )

    features: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    monthly_token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_processing_basis: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'consent_required'")
    )
    # Epic 53 (400_04): enablement-notice acknowledgement (master switch), tenant / platform-tenant kill switches and
    # the deep-tier (Opus) opt-in.
    agentic_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agentic_enabled_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    agentic_notice_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    kill_switch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    kill_switch_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kill_switch_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    platform_kill_switch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    platform_kill_switch_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    platform_kill_switch_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    deep_tier_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class AIBudgetAlert(UUIDPk, TenantMixin, Base):
    """One row per (tenant, month, threshold) so admins/operators are alerted once (Epic 12, FR-63)."""

    __tablename__ = "ai_budget_alerts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period", "threshold"),
        CheckConstraint("threshold IN (80, 100, 120)", name="threshold"),
    )

    period: Mapped[str] = mapped_column(String(7), nullable=False)  # "YYYY-MM"
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PlatformAIControls(Base):
    """Platform AI controls (Epic 53, 400_03): one row ``id=1``; non-RLS platform table (SELECT/UPDATE for
    ``talent_app``). Global kill switch (FR-271) and the provider-outage marker (FR-275)."""

    __tablename__ = "platform_ai_controls"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    global_kill_switch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    global_kill_switch_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    global_kill_switch_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_down_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
