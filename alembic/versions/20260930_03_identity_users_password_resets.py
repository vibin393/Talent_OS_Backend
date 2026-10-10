"""Identity: users + password_resets (tenant-owned, RLS), sign-in lockout columns, tenant session overrides.

Revision ID: 20260930_03
Revises: 20260930_02
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20260930_03"
down_revision: str | None = "20260930_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lockout_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("failed_login_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    # ---- platform tables: tenant session overrides, operator lockout
    op.add_column("tenants", sa.Column("session_idle_minutes", sa.Integer(), nullable=True))
    op.add_column("tenants", sa.Column("session_absolute_hours", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_tenants_session_idle"),
        "tenants",
        "session_idle_minutes IS NULL OR session_idle_minutes BETWEEN 1 AND 1440",
    )
    op.create_check_constraint(
        op.f("ck_tenants_session_absolute"),
        "tenants",
        "session_absolute_hours IS NULL OR session_absolute_hours BETWEEN 1 AND 720",
    )
    for column in _lockout_columns():
        op.add_column("platform_operators", column)

    # ---- tenant-owned
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "role_keys",
            postgresql.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mfa_secret", sa.String(length=255), nullable=True),
        sa.Column("mfa_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_lockout_columns(),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_users_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("tenant_id", "email", name=op.f("uq_users_tenant_id_email")),
    )
    op.create_index(op.f("ix_users_tenant_id"), "users", ["tenant_id"])
    enable_rls("users")

    op.create_table(
        "password_resets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_password_resets_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_password_resets_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_resets")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_password_resets_token_hash")),
    )
    op.create_index(op.f("ix_password_resets_tenant_id"), "password_resets", ["tenant_id"])
    op.create_index(op.f("ix_password_resets_user_id"), "password_resets", ["user_id"])
    enable_rls("password_resets")
    # Grants: covered by the baseline's ALTER DEFAULT PRIVILEGES (tables created by the owner role).


def downgrade() -> None:
    op.drop_table("password_resets")  # drops its RLS policy and indexes with it
    op.drop_table("users")
    for column in ("locked_until", "first_failed_at", "failed_login_count"):
        op.drop_column("platform_operators", column)
    op.drop_constraint(op.f("ck_tenants_session_absolute"), "tenants", type_="check")
    op.drop_constraint(op.f("ck_tenants_session_idle"), "tenants", type_="check")
    op.drop_column("tenants", "session_absolute_hours")
    op.drop_column("tenants", "session_idle_minutes")
