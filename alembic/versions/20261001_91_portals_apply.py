"""Epic 9 apply: ``apply_sessions`` and ``application_answers`` (tenant-owned, RLS) and the platform lookup
``apply_session_lookups`` (token hash -> tenant + session; no PII, non-RLS, listed in ``core.tenancy``).

Revision ID: 20261001_91
Revises: 20261001_90
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_91"
down_revision: str | tuple[str, ...] | None = "20261001_90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("apply_sessions", "application_answers")


def _timestamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "apply_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("posting_id", sa.Uuid(), nullable=False),
        sa.Column(
            "consents",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("resume_file_id", sa.Uuid(), nullable=True),
        sa.Column("upload_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("resume_text", sa.Text(), nullable=True),
        sa.Column("text_status", sa.String(length=20), nullable=True),
        sa.Column("parse_status", sa.String(length=20), nullable=True),
        sa.Column("parsed_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("prefill", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ai_action_id", sa.Uuid(), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("confirmation_email", postgresql.CITEXT(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "parse_status IS NULL OR parse_status IN ('pending', 'parsed', 'failed_manual', 'unavailable', "
            "'skipped_disabled', 'skipped_consent')",
            name=op.f("ck_apply_sessions_parse_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_apply_sessions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_apply_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_apply_sessions_token_hash")),
    )
    op.create_index(op.f("ix_apply_sessions_tenant_id"), "apply_sessions", ["tenant_id"], unique=False)
    op.create_index("ix_apply_sessions_expires_at", "apply_sessions", ["expires_at"], unique=False)
    op.create_index("ix_apply_sessions_application_id", "apply_sessions", ["application_id"], unique=False)

    op.create_table(
        "apply_session_lookups",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_apply_session_lookups")),
    )
    op.create_index(
        op.f("ix_apply_session_lookups_tenant_id"), "apply_session_lookups", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_apply_session_lookups_session_id"), "apply_session_lookups", ["session_id"], unique=False
    )

    op.create_table(
        "application_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column(
            "questions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "answers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_application_answers_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_answers")),
        sa.UniqueConstraint("application_id", name=op.f("uq_application_answers_application_id")),
    )
    op.create_index(
        op.f("ix_application_answers_tenant_id"), "application_answers", ["tenant_id"], unique=False
    )

    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_rls(table)
    op.drop_table("application_answers")
    op.drop_table("apply_session_lookups")
    op.drop_table("apply_sessions")
