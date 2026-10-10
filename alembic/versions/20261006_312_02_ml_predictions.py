"""Epic 48 story 48.3 (FR-241..243, AD-45): ``ml_predictions`` -- the latest prediction per (subject, model).

Subjects are polymorphic ids (demand / offer / placement), validated in the service (no cross-branch FK).
``candidate_id`` is the erasure key for offer / placement subjects. Tenant RLS only.

Revision ID: 20261006_312_02_ml_predictions
Revises: 20261006_312_01_ml_store
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261006_312_02_ml_predictions"
down_revision: str | tuple[str, ...] | None = "20261006_312_01_ml_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    t = "ml_predictions"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("subject_kind", sa.String(10), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("score", sa.Numeric(10, 4), nullable=False),
        sa.Column("predicted_days", sa.Numeric(8, 1), nullable=True),
        sa.Column("band", sa.String(10), nullable=True),
        sa.Column("interval", postgresql.JSONB(), nullable=True),
        sa.Column("contributions", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("explanation_text", sa.Text(), nullable=True),
        sa.Column("explanation_ai_action_id", sa.Uuid(), nullable=True),
        sa.Column("explanation_status", sa.String(20), server_default=sa.text("'none'"), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ml_predictions"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ml_predictions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "subject_kind", "subject_id", "model_id", name="uq_ml_predictions_subject_model"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_ml_predictions_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "model_id"],
            ["ml_models.tenant_id", "ml_models.id"],
            name="fk_ml_predictions_model_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('time_to_fill', 'offer_drop', 'attrition')", name=op.f("ck_ml_predictions_kind")
        ),
        sa.CheckConstraint(
            "subject_kind IN ('demand', 'offer', 'placement')", name=op.f("ck_ml_predictions_subject_kind")
        ),
        sa.CheckConstraint(
            "band IS NULL OR band IN ('low', 'medium', 'high')", name=op.f("ck_ml_predictions_band")
        ),
        sa.CheckConstraint(
            "explanation_status IN ('none', 'ok', 'unavailable')",
            name=op.f("ck_ml_predictions_explanation_status"),
        ),
    )
    op.create_index("ix_ml_predictions_tenant_id", t, ["tenant_id"])
    op.create_index(
        "ix_ml_predictions_subject",
        t,
        ["tenant_id", "subject_kind", "subject_id", sa.text("computed_at DESC")],
    )
    op.create_index("ix_ml_predictions_candidate_id", t, ["candidate_id"])
    enable_rls(t)


def downgrade() -> None:
    disable_rls("ml_predictions")
    op.drop_table("ml_predictions")
