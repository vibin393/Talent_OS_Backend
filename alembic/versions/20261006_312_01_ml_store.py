# ruff: noqa: E501
"""Epic 48 story 48.1 (FR-244, AD-45): ``analytics_settings``, ``ml_feature_specs``, ``ml_training_sets`` and
``ml_models``.

Tenant RLS only (no portal reads these tables). Thresholds can only be raised above the platform defaults
(``min_samples >= 200``, ``min_minority >= 30``); at most one ``active`` model per kind (partial UNIQUE).

Revision ID: 20261006_312_01_ml_store
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261006_312_01_ml_store"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_analytics",)
depends_on: str | Sequence[str] | None = None

KINDS = "('time_to_fill', 'offer_drop', 'attrition')"
STATUSES = "('training', 'active', 'insufficient_data', 'failed', 'superseded', 'disabled')"
ENABLED = """'{"time_to_fill": true, "offer_drop": true, "attrition": true}'::jsonb"""
BANDS = """'{"offer_drop": {"medium": "0.25", "high": "0.50"}, "attrition": {"medium": "0.25", "high": "0.50"}}'::jsonb"""


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def upgrade() -> None:
    t = "analytics_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("enabled_kinds", postgresql.JSONB(), server_default=sa.text(ENABLED), nullable=False),
        sa.Column("min_samples", sa.Integer(), server_default=sa.text("200"), nullable=False),
        sa.Column("min_minority", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("band_thresholds", postgresql.JSONB(), server_default=sa.text(BANDS), nullable=False),
        sa.Column("history_months", sa.Integer(), server_default=sa.text("36"), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_analytics_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_analytics_settings_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("min_samples >= 200", name=op.f("ck_analytics_settings_min_samples")),
        sa.CheckConstraint("min_minority >= 30", name=op.f("ck_analytics_settings_min_minority")),
        sa.CheckConstraint(
            "history_months BETWEEN 6 AND 120", name=op.f("ck_analytics_settings_history_months")
        ),
    )
    enable_rls(t)

    t = "ml_feature_specs"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("features", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ml_feature_specs"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_ml_feature_specs_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("tenant_id", "kind", "version", name="uq_ml_feature_specs_kind_version"),
        sa.CheckConstraint(f"kind IN {KINDS}", name=op.f("ck_ml_feature_specs_kind")),
    )
    op.create_index("ix_ml_feature_specs_tenant_id", t, ["tenant_id"])
    enable_rls(t)

    t = "ml_training_sets"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("model_kind", sa.String(20), nullable=False),
        sa.Column("spec_version", sa.Integer(), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("n_minority", sa.Integer(), nullable=True),
        sa.Column("storage_key", sa.String(200), nullable=False),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("holdout_from", sa.Date(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rewritten_for_erasure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "candidate_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.CheckConstraint(f"model_kind IN {KINDS}", name=op.f("ck_ml_training_sets_model_kind")),
        sa.CheckConstraint("format IN ('parquet', 'csv')", name=op.f("ck_ml_training_sets_format")),
    )
    op.create_index("ix_ml_training_sets_tenant_id", t, ["tenant_id"])
    op.create_index("ix_ml_training_sets_expires_at", t, ["expires_at"])
    enable_rls(t)

    t = "ml_models"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'training'"), nullable=False),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("n_samples", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("n_minority", sa.Integer(), nullable=True),
        sa.Column("training_set_id", sa.Uuid(), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("feature_spec_version", sa.Integer(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("failure_code", sa.String(40), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "kind", "version", name="uq_ml_models_kind_version"),
        sa.CheckConstraint(f"kind IN {KINDS}", name=op.f("ck_ml_models_kind")),
        sa.CheckConstraint(f"status IN {STATUSES}", name=op.f("ck_ml_models_status")),
    )
    op.create_index("ix_ml_models_tenant_id", t, ["tenant_id"])
    op.create_index(
        "uq_ml_models_one_active",
        t,
        ["tenant_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    enable_rls(t)


def downgrade() -> None:
    for t in ("ml_models", "ml_training_sets", "ml_feature_specs", "analytics_settings"):
        disable_rls(t)
        op.drop_table(t)
