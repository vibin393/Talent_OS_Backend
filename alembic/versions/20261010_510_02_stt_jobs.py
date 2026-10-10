# ruff: noqa: E501
"""Epic 76 (story 76.3, spec A1/A5): ``stt_jobs`` -- transcription jobs for call recordings, P4 voice uploads and
interview captures. Tenant table with RLS + org RLS.

Revision ID: 20261010_510_02_stt_jobs
Revises: 20261010_510_01_telephony
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_510_02_stt_jobs"
down_revision: str | tuple[str, ...] | None = "20261010_510_01_telephony"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    t = "stt_jobs"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name="fk_stt_jobs_tenant_id_tenants"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source_kind", sa.String(20), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("voice_session_id", sa.Uuid(), nullable=True),
        sa.Column("interview_capture_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=True),
        sa.Column("provider_key", sa.String(20), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("language_hints", postgresql.ARRAY(sa.String(10)), server_default=sa.text("'{}'::varchar(10)[]"), nullable=False),
        sa.Column("channels", sa.SmallInteger(), nullable=True),
        sa.Column("audio_seconds", sa.Integer(), nullable=True),
        sa.Column("billable_minutes", sa.Integer(), nullable=True),
        sa.Column("cost_estimate_minor", sa.BigInteger(), nullable=True),
        sa.Column("transcript_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_stt_jobs_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "source_kind", "source_id", "file_id", name="uq_stt_jobs_source"),
        sa.CheckConstraint("source_kind IN ('voice_call', 'voice_upload', 'interview_capture')", name=op.f("ck_stt_jobs_source_kind")),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'skipped')", name=op.f("ck_stt_jobs_status")),
    )
    op.create_index("ix_stt_jobs_status_created", t, ["status", "created_at"])
    op.create_index("ix_stt_jobs_candidate_id", t, ["candidate_id"])
    op.create_index("ix_stt_jobs_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    disable_org_rls("stt_jobs")
    disable_rls("stt_jobs")
    op.drop_table("stt_jobs")
