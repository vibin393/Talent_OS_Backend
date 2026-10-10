"""Core baseline: extensions, talent_app runtime role + grants, tenant_sequences (RLS).

Revision ID: 20260930_01
Revises:
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import (
    create_app_role,
    enable_rls,
    grant_app_privileges,
    revoke_app_privileges,
)
from app.core.settings import get_settings

revision: str = "20260930_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = ("citext", "pg_trgm", "unaccent", "pgcrypto")


def upgrade() -> None:
    for ext in EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")

    settings = get_settings()
    create_app_role(settings.db_app_user, settings.db_app_password)

    op.create_table(
        "tenant_sequences",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "key", name="pk_tenant_sequences"),
    )
    enable_rls("tenant_sequences")

    # After table creation so GRANT ... ON ALL TABLES covers it; default privileges cover later tables.
    grant_app_privileges(settings.db_app_user)


def downgrade() -> None:
    op.drop_table("tenant_sequences")
    # The role is cluster-wide (shared by dev/test DBs) and extensions may be shared: keep both.
    revoke_app_privileges(get_settings().db_app_user)
