"""Core audit log (story 1.6): append-only ``audit_logs`` (mixed tenant/platform RLS, INSERT/SELECT only for
the runtime role) and ``subject_keys`` (per-subject data keys for crypto-shredding, tenant RLS).

Revision ID: 20260930_05
Revises: 20260930_04
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_mixed_rls, enable_rls
from app.core.settings import get_settings

revision: str = "20260930_05"
down_revision: str | None = "20260930_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTOR_TYPES = "('user', 'candidate', 'platform', 'system', 'ai', 'workflow', 'api_key')"


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.CheckConstraint(f"actor_type IN {ACTOR_TYPES}", name=op.f("ck_audit_logs_actor_type")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_audit_logs_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.execute("CREATE INDEX ix_audit_logs_tenant_id_occurred_at ON audit_logs (tenant_id, occurred_at DESC)")
    op.create_index("ix_audit_logs_entity", "audit_logs", ["entity_type", "entity_id"])
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    enable_mixed_rls("audit_logs")

    op.create_table(
        "subject_keys",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("key_encrypted", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_subject_keys_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("tenant_id", "subject_type", "subject_id", name=op.f("pk_subject_keys")),
    )
    enable_rls("subject_keys")

    # Default privileges gave the runtime role full DML: audit rows are append-only, keys are never deleted.
    role = get_settings().db_app_user
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM {role}")
    op.execute(f"REVOKE DELETE, TRUNCATE ON subject_keys FROM {role}")


def downgrade() -> None:
    op.drop_table("subject_keys")
    op.drop_index("ix_audit_logs_actor_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity", table_name="audit_logs")
    op.execute("DROP INDEX IF EXISTS ix_audit_logs_tenant_id_occurred_at")
    op.drop_table("audit_logs")
