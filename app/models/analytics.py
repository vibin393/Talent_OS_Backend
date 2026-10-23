# ruff: file-ignore[line-too-long]
"""Predictive analytics (Epic 48, branch ``p3_analytics``, migrations ``20261006_312_01..02``; FR-241..245, AD-45).

* ``analytics_settings`` -- one row per tenant: enabled kinds, thresholds (only raisable above the platform
  defaults 200 / 30), band thresholds and the training history window. Audited.
* ``ml_feature_specs`` -- the code-defined feature spec (``services/analytics/features/specs.py``) as inserted
  per tenant on the first training of a version (model-card / audit evidence).
* ``ml_training_sets`` -- one row per stored snapshot (Parquet / CSV under the tenant storage prefix); 90-day
  expiry; ``candidate_ids`` lets erasure find and rewrite the snapshots that hold a subject.
* ``ml_models`` -- one row per training attempt (``training | active | insufficient_data | failed | superseded |
  disabled``); at most one ``active`` per kind (partial UNIQUE). Coefficients / metrics are decimal strings in
  JSON (no floats persisted). Audited.
* ``ml_predictions`` -- the latest prediction per (subject, model): score, band / interval, labelled
  contributions and the lazily generated AI explanation. ``candidate_id`` (offer / placement subjects) is the
  erasure key.

Every table is tenant-owned with tenant RLS only (no portal ever reads them).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

MODEL_KINDS = ("time_to_fill", "offer_drop", "attrition")
CLASSIFICATION_KINDS = ("offer_drop", "attrition")
SUBJECT_KINDS = ("demand", "offer", "placement")
MODEL_STATUSES = ("training", "active", "insufficient_data", "failed", "superseded", "disabled")
BANDS = ("low", "medium", "high")
EXPLANATION_STATUSES = ("none", "ok", "unavailable")
SNAPSHOT_FORMATS = ("parquet", "csv")

PLATFORM_MIN_SAMPLES = 200
PLATFORM_MIN_MINORITY = 30
DEFAULT_ENABLED_KINDS: dict[str, bool] = dict.fromkeys(MODEL_KINDS, True)
DEFAULT_BAND_THRESHOLDS: dict[str, dict[str, str]] = {
    "offer_drop": {"medium": "0.25", "high": "0.50"},
    "attrition": {"medium": "0.25", "high": "0.50"},
}
DEFAULT_HISTORY_MONTHS = 36


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AnalyticsSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created lazily with the defaults)."""

    __tablename__ = "analytics_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint(f"min_samples >= {PLATFORM_MIN_SAMPLES}", name="min_samples"),
        CheckConstraint(f"min_minority >= {PLATFORM_MIN_MINORITY}", name="min_minority"),
        CheckConstraint("history_months BETWEEN 6 AND 120", name="history_months"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    enabled_kinds: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    min_samples: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(PLATFORM_MIN_SAMPLES))
    )
    min_minority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(PLATFORM_MIN_MINORITY))
    )
    band_thresholds: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    history_months: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_HISTORY_MONTHS))
    )


class MlFeatureSpec(UUIDPk, TenantMixin, Base):
    __tablename__ = "ml_feature_specs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "version", name="uq_ml_feature_specs_kind_version"),
        CheckConstraint(_in("kind", MODEL_KINDS), name="kind"),
    )

    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    features: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MlTrainingSet(UUIDPk, TenantMixin, Base):
    __tablename__ = "ml_training_sets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("model_kind", MODEL_KINDS), name="model_kind"),
        CheckConstraint(_in("format", SNAPSHOT_FORMATS), name="format"),
        Index("ix_ml_training_sets_expires_at", "expires_at"),
    )

    model_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    n_minority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_key: Mapped[str] = mapped_column(String(200), nullable=False)
    format: Mapped[str] = mapped_column(String(10), nullable=False)
    holdout_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rewritten_for_erasure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    candidate_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )


class MlModel(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "ml_models"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "kind", "version", name="uq_ml_models_kind_version"),
        CheckConstraint(_in("kind", MODEL_KINDS), name="kind"),
        CheckConstraint(_in("status", MODEL_STATUSES), name="status"),
        Index(
            "uq_ml_models_one_active",
            "tenant_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
    __audit_exclude__ = ("params",)

    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'training'"))
    trained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    n_samples: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    n_minority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    training_set_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    feature_spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    failure_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MlPrediction(UUIDPk, TenantMixin, Base):
    __tablename__ = "ml_predictions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id", "subject_kind", "subject_id", "model_id", name="uq_ml_predictions_subject_model"
        ),
        CheckConstraint(_in("kind", MODEL_KINDS), name="kind"),
        CheckConstraint(_in("subject_kind", SUBJECT_KINDS), name="subject_kind"),
        CheckConstraint(f"band IS NULL OR {_in('band', BANDS)}", name="band"),
        CheckConstraint(_in("explanation_status", EXPLANATION_STATUSES), name="explanation_status"),
        Index(
            "ix_ml_predictions_subject", "tenant_id", "subject_kind", "subject_id", text("computed_at DESC")
        ),
        Index("ix_ml_predictions_candidate_id", "candidate_id"),
        ForeignKeyConstraint(
            ["tenant_id", "model_id"],
            ["ml_models.tenant_id", "ml_models.id"],
            name="fk_ml_predictions_model_id",
            ondelete="CASCADE",
        ),
    )

    model_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    score: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    predicted_days: Mapped[Decimal | None] = mapped_column(Numeric(8, 1), nullable=True)
    band: Mapped[str | None] = mapped_column(String(10), nullable=True)
    interval: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    contributions: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    explanation_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation_ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    explanation_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'none'"))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
