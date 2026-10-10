"""Identity: tenants, platform_operators, sessions (platform, no RLS) + invitations (tenant-owned, RLS).

Revision ID: 20260930_02
Revises: 20260930_01
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20260930_02"
down_revision: str | None = "20260930_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    # ---- platform tables (no RLS; listed in app/core/tenancy.py PLATFORM_TABLES)
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", postgresql.CITEXT(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("seat_limit", sa.Integer(), nullable=False),
        sa.Column("mfa_required", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("status IN ('active', 'suspended')", name=op.f("ck_tenants_status")),
        sa.CheckConstraint("seat_limit > 0", name=op.f("ck_tenants_seat_limit_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenants_slug")),
    )

    op.create_table(
        "platform_operators",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("mfa_secret", sa.String(length=255), nullable=True),
        sa.Column("mfa_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_operators")),
        sa.UniqueConstraint("email", name=op.f("uq_platform_operators_email")),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("principal_type", sa.String(length=16), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.CheckConstraint(
            "principal_type IN ('user', 'candidate', 'platform')", name=op.f("ck_sessions_principal_type")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index("ix_sessions_principal", "sessions", ["principal_type", "principal_id"])
    op.create_index(op.f("ix_sessions_tenant_id"), "sessions", ["tenant_id"])

    # ---- tenant-owned
    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("role_key", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invited_by_type", sa.String(length=16), nullable=False),
        sa.Column("invited_by_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "invited_by_type IN ('user', 'platform')", name=op.f("ck_invitations_invited_by_type")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_invitations_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invitations_token_hash")),
    )
    op.create_index(op.f("ix_invitations_tenant_id"), "invitations", ["tenant_id"])
    enable_rls("invitations")

    # tenant_sequences (core baseline) predates tenants: attach its FK now. Rows can only be orphans here
    # (tenants did not exist before this revision), so they are removed first.
    op.execute("DELETE FROM tenant_sequences")
    op.create_foreign_key(
        op.f("fk_tenant_sequences_tenant_id_tenants"),
        "tenant_sequences",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Grants: covered by the baseline's ALTER DEFAULT PRIVILEGES (tables created by the owner role).


def downgrade() -> None:
    op.drop_constraint(op.f("fk_tenant_sequences_tenant_id_tenants"), "tenant_sequences", type_="foreignkey")
    op.drop_table("invitations")  # drops its RLS policy and indexes with it
    op.drop_index(op.f("ix_sessions_tenant_id"), table_name="sessions")
    op.drop_index("ix_sessions_principal", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("platform_operators")
    op.drop_table("tenants")
