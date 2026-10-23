# ruff: file-ignore[line-too-long]
"""Bias audit & adverse-impact reporting (Epic 49, branch ``p3_fairness``, migrations ``20261007_313_01..03``;
FR-246..FR-249, AD-47, NFR-38).

**Isolation:** this module is NOT imported by ``app/models/__init__.py``. Only ``app.services.fairness.*``,
``app.services.fairness_erasure`` and the fairness routers / task may import it (import-linter contract
``fairness-isolation``); ``alembic/env.py`` imports it explicitly and the fairness routers import it at app start so
the tables are on ``Base.metadata``.

* ``fairness_settings`` -- one row per tenant (self-ID switches, k >= 5, LL144 texts, notice lead days >= 10).
* ``demographic_category_sets`` / ``demographic_dimensions`` -- the versioned category set; dimension keys are
  limited by a DB CHECK to ``gender``, ``age_band``, ``disability`` (no caste, religion or race/ethnicity).
* ``demographic_self_ids`` -- one sealed answer document per Candidate (``EncryptedJSON``, key purpose
  ``demographic``). Not audited (nothing about the answers ever reaches the audit log). RLS: tenant + org
  (worker = own row) + RESTRICTIVE ``fairness_access`` (internal sessions see nothing unless the aggregate task,
  erasure or the apply-form write sets ``app.fairness_access``).
* ``self_id_apply_uses`` -- one self-ID submission per apply session.
* ``fairness_runs`` -- aggregate-only report snapshots (no candidate ids).
* ``aedt_inventory`` / ``fairness_publications`` -- AEDT inventory and public summaries.
* ``aedt_jurisdiction_flags`` / ``aedt_alternative_requests`` -- NYC LL144 notice and alternative-process requests.
"""

import uuid
from datetime import date, datetime
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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted import EncryptedJSON, Sealed
from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

DIMENSION_KEYS = ("gender", "age_band", "disability")
PREFER_NOT = "prefer_not_to_say"
SET_STATUSES = ("active", "retired")
SELF_ID_SOURCES = ("apply", "portal")
SELECTION_POINTS = ("stage_transition", "ai_match_threshold", "ai_screening_pass", "assessment_pass")
# Epic 65 (412_02, C-16): Phase 4 agent selection points (implemented in ``fairness.aedt_p4``).
SELECTION_POINTS += ("agent_shortlist_added", "agent_screening_flag_advanced", "agent_draft_rating_accepted")  # type: ignore[assignment]
RUN_METHODS = ("selection_rate", "scoring_rate", "both")
RUN_STATUSES = ("queued", "running", "completed", "failed")
AEDT_FEATURES = ("ai_match", "ai_screening", "assessment_autoscore", "other")
# Epic 53 (400_01, AD-71): Phase 4 agent features (inventory entries are seeded by Epic 65).
AEDT_FEATURES += (  # type: ignore[assignment]
    "agent_sourcing", "agent_screening", "agent_voice_screening", "agent_interview_assist",
)  # fmt: skip
AEDT_STATUSES = ("active", "retired", "inactive")
JURISDICTIONS = ("nyc",)
ALT_CHANNELS = ("apply", "portal")
ALT_STATUSES = ("open", "handled")
MIN_K = 5
MIN_NOTICE_DAYS = 10


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(
    column: str, target: str, *, ondelete: str = "RESTRICT", name: str | None = None
) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class FairnessSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "fairness_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint(f"k >= {MIN_K}", name="k"),
        CheckConstraint(f"notice_lead_business_days >= {MIN_NOTICE_DAYS}", name="notice_days"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    self_id_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    self_id_on_apply: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    self_id_in_portal: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    category_set_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    k: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text(str(MIN_K)))
    ll144_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ll144_notice_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    alternative_process_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_retention_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualifications_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    notice_lead_business_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text(str(MIN_NOTICE_DAYS))
    )


class DemographicCategorySet(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "demographic_category_sets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "key", "version", name="uq_demographic_category_sets_version"),
        CheckConstraint(_in("status", SET_STATUSES), name="status"),
    )

    key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class DemographicDimension(UUIDPk, TenantMixin, Base):
    __tablename__ = "demographic_dimensions"
    __table_args__ = (
        UniqueConstraint("set_id", "key", name="uq_demographic_dimensions_set_key"),
        CheckConstraint(_in("key", DIMENSION_KEYS), name="key"),
        _tfk("set_id", "demographic_category_sets", ondelete="CASCADE"),
        Index("ix_demographic_dimensions_set_id", "set_id"),
    )

    set_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    options: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)


class DemographicSelfId(UUIDPk, TenantMixin, Base):
    """Sealed self-ID answers. Never audited, never logged, never exported (only the Candidate reads them back)."""

    __tablename__ = "demographic_self_ids"
    __audit_exclude__ = ("answers",)
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_demographic_self_ids_candidate_id"),
        CheckConstraint(_in("source", SELF_ID_SOURCES), name="source"),
        _tfk("candidate_id", "candidates", ondelete="CASCADE"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    set_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    set_version: Mapped[int] = mapped_column(Integer, nullable=False)
    answers: Mapped[Sealed] = mapped_column(EncryptedJSON(), nullable=False)
    consent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("consents.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SelfIdApplyUse(TenantMixin, Base):
    """One self-ID submission per apply session (the apply token is single use for self-ID)."""

    __tablename__ = "self_id_apply_uses"

    apply_session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("apply_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FairnessRun(UUIDPk, TenantMixin, Base):
    """Aggregate-only report snapshot (no candidate ids, no per-person rows)."""

    __tablename__ = "fairness_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("selection_point", SELECTION_POINTS), name="selection_point"),
        CheckConstraint(_in("method", RUN_METHODS), name="method"),
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        CheckConstraint(f"k >= {MIN_K}", name="k"),
        CheckConstraint("date_from <= date_to", name="dates"),
        _tfk("created_by", "users"),
        Index("ix_fairness_runs_created_at", "created_at"),
    )

    selection_point: Mapped[str] = mapped_column(String(30), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    intersections: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    k: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'queued'"))
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    total_applicants: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unknown_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    pack: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)


class AedtInventory(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "aedt_inventory"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("feature", AEDT_FEATURES), name="feature"),
        CheckConstraint(_in("status", AEDT_STATUSES), name="status"),
    )

    feature: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str | None] = mapped_column(String(60), nullable=True)
    first_used_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_categories: Mapped[list[str]] = mapped_column(
        ARRAY(String(200)), nullable=False, server_default=text("'{}'::varchar[]")
    )
    used_to_filter: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'inactive'"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(40), nullable=True)


class FairnessPublication(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "fairness_publications"
    __audit_exclude__ = ("summary",)
    __table_args__ = (
        _tfk("run_id", "fairness_runs"),
        _tfk("aedt_id", "aedt_inventory"),
        _tfk("published_by", "users"),
        CheckConstraint("audit_date <= distribution_date", name="dates"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    aedt_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    audit_date: Mapped[date] = mapped_column(Date, nullable=False)
    distribution_date: Mapped[date] = mapped_column(Date, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    unpublished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AedtJurisdictionFlag(TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "aedt_jurisdiction_flags"
    __audit_entity__ = "demand"
    __audit_entity_id__ = "demand_id"
    __table_args__ = (
        CheckConstraint(_in("jurisdiction", JURISDICTIONS), name="jurisdiction"),
        _tfk("demand_id", "demands", ondelete="CASCADE"),
        _tfk("flagged_by", "users"),
    )

    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    jurisdiction: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'nyc'"))
    notice_published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    flagged_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class AedtAlternativeRequest(UUIDPk, TenantMixin, Base):
    """Alternative selection process request (follows the Application; erased with the Candidate)."""

    __tablename__ = "aedt_alternative_requests"
    __table_args__ = (
        UniqueConstraint("application_id", name="uq_aedt_alternative_requests_app"),
        CheckConstraint(_in("channel", ALT_CHANNELS), name="channel"),
        CheckConstraint(_in("status", ALT_STATUSES), name="status"),
        _tfk("application_id", "applications", ondelete="CASCADE"),
        _tfk("candidate_id", "candidates", ondelete="CASCADE"),
        _tfk("demand_id", "demands", ondelete="CASCADE"),
        Index("ix_aedt_alternative_requests_cand", "candidate_id"),
        Index("ix_aedt_alternative_requests_demand", "demand_id"),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    demand_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'open'"))
