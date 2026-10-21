# ruff: noqa: E501
"""Unified platform hub ingest: inbound data loops (Croar assessments, Feedback IQ engagement signals).

Two tenant-owned (forced RLS) tables:

* ``candidate_assessment`` — assessment results from Croar, linked to a Candidate (+ optional Application).
* ``engagement_signal`` — engagement / feedback signals from Feedback IQ, best-effort matched to a Candidate.

Both are idempotent on ``event_id`` (unique per tenant). Composite ``(tenant_id, fk) -> target(tenant_id, id)``
foreign keys keep references tenant-safe (FK checks bypass RLS). Chains onto the current head so a plain
``alembic upgrade heads`` (and the ``tenant_template`` migrate) applies it last.

Revision ID: 20261010_514_01_ingest_loops
Revises: 20261010_513_02_role_mcaps
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261010_514_01_ingest_loops"
down_revision: str | tuple[str, ...] | None = "20261010_513_02_role_mcaps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    # ---------------------------------------------------------------- candidate_assessment
    t = "candidate_assessment"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(60), nullable=False),
        sa.Column("provider", sa.String(120), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("score", sa.Numeric(12, 4), nullable=True),
        sa.Column("max_score", sa.Numeric(12, 4), nullable=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"], ["candidates.tenant_id", "candidates.id"],
            name=f"fk_{t}_candidate_id_candidates", ondelete="CASCADE",
        ),
        # Simple FK: ON DELETE SET NULL nulls only application_id (a composite FK would also null the NOT NULL tenant_id).
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"],
            name=f"fk_{t}_application_id_applications", ondelete="SET NULL",
        ),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "event_id", name=f"uq_{t}_tenant_id_event_id"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index(f"ix_{t}_candidate_id", t, ["candidate_id"])
    enable_rls(t)

    # ---------------------------------------------------------------- engagement_signal
    t = "engagement_signal"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("subject_email", sa.String(320), nullable=True),
        sa.Column("subject_ats_candidate_id", sa.String(128), nullable=True),
        sa.Column("matched_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(60), nullable=False),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("score", sa.Numeric(12, 4), nullable=True),
        sa.Column("summary", sa.String(2000), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"),
        # Simple FK: ON DELETE SET NULL nulls only matched_candidate_id (the signal is kept regardless of a match).
        sa.ForeignKeyConstraint(
            ["matched_candidate_id"], ["candidates.id"],
            name=f"fk_{t}_matched_candidate_id_candidates", ondelete="SET NULL",
        ),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "event_id", name=f"uq_{t}_tenant_id_event_id"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index(f"ix_{t}_matched_candidate_id", t, ["matched_candidate_id"])
    op.create_index(f"ix_{t}_subject_email", t, ["subject_email"])
    enable_rls(t)


def downgrade() -> None:
    op.drop_table("engagement_signal")
    op.drop_table("candidate_assessment")
