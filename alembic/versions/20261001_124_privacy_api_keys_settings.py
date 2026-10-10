"""Epic 12 API keys + settings hub: ``api_keys``, ``api_key_lookups`` (platform), ``api_key_request_log``,
``tenant_settings`` (backfilled) and ``expires_at`` indexes for the cleanup jobs.

Revision ID: 20261001_124
Revises: 20261001_123
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_124"
down_revision: str | tuple[str, ...] | None = "20261001_123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column("key_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("scopes", sa.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("rate_limit_per_min", sa.Integer(), server_default=sa.text("600"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(length=64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.Column("rotated_from_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("rate_limit_per_min BETWEEN 60 AND 6000", name=op.f("ck_api_keys_rate_limit")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_api_keys_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_api_keys_tenant_id_id")),
    )
    op.create_index(op.f("ix_api_keys_tenant_id"), "api_keys", ["tenant_id"], unique=False)
    enable_rls("api_keys")
    op.create_table(
        "api_key_lookups",
        sa.Column("key_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("key_hash", name=op.f("pk_api_key_lookups")),
    )
    op.create_index(op.f("ix_api_key_lookups_tenant_id"), "api_key_lookups", ["tenant_id"], unique=False)
    op.create_table(
        "api_key_request_log",
        sa.Column("key_id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("route_template", sa.String(length=300), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "key_id"],
            ["api_keys.tenant_id", "api_keys.id"],
            name="fk_api_key_request_log_key_id_api_keys",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_api_key_request_log_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_key_request_log")),
    )
    op.create_index("ix_api_key_request_log_at", "api_key_request_log", ["at"], unique=False)
    op.create_index("ix_api_key_request_log_key_at", "api_key_request_log", ["key_id", "at"], unique=False)
    op.create_index(
        op.f("ix_api_key_request_log_tenant_id"), "api_key_request_log", ["tenant_id"], unique=False
    )
    enable_rls("api_key_request_log")
    op.create_table(
        "tenant_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "company",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "regional",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "id_prefixes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "link_expiries",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_settings_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenant_settings")),
    )
    enable_rls("tenant_settings")
    op.create_index(op.f("ix_invitations_expires_at"), "invitations", ["expires_at"], unique=False)
    op.create_index(op.f("ix_password_resets_expires_at"), "password_resets", ["expires_at"], unique=False)
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)
    # Backfill one settings row per tenant (owner role; tenant context set per row for the RLS check).
    # pipeline_settings (Epic 6) does not exist yet, so its documented link-day defaults are copied.
    bind = op.get_bind()
    for (tenant_id,) in bind.execute(sa.text("SELECT id FROM tenants")).all():
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        bind.execute(
            sa.text(
                "INSERT INTO tenant_settings (tenant_id, link_expiries) "
                "VALUES (:tid, CAST(:le AS jsonb)) ON CONFLICT DO NOTHING"
            ),
            {
                "tid": tenant_id,
                "le": '{"consent_request": 7, "ai_screening": 7, "client_feedback": 14, "dsr_export": 7}',
            },
        )
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index(op.f("ix_password_resets_expires_at"), table_name="password_resets")
    op.drop_index(op.f("ix_invitations_expires_at"), table_name="invitations")
    disable_rls("tenant_settings")
    op.drop_table("tenant_settings")
    disable_rls("api_key_request_log")
    op.drop_table("api_key_request_log")
    op.drop_table("api_key_lookups")
    disable_rls("api_keys")
    op.drop_table("api_keys")
