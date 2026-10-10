"""Epic 7 calendar / mailbox OAuth connections (tenant-owned, RLS): user_connections (tokens encrypted).

Revision ID: 20261001_72
Revises: 20261001_71
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_72"
down_revision: str | tuple[str, ...] | None = '20261001_71'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ('user_connections',)


def upgrade() -> None:
    op.create_table('user_connections',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('provider', sa.String(length=20), nullable=False),
    sa.Column('account_email', postgresql.CITEXT(), nullable=True),
    sa.Column('scopes', postgresql.ARRAY(sa.String(length=300)), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('access_token_enc', sa.LargeBinary(), nullable=True),
    sa.Column('refresh_token_enc', sa.LargeBinary(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'active'"), nullable=False),
    sa.Column('connected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_error', sa.String(length=200), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("provider IN ('google', 'microsoft', 'fake')", name=op.f('ck_user_connections_provider')),
    sa.CheckConstraint("status IN ('active', 'needs_reconnect', 'revoked')", name=op.f('ck_user_connections_status')),
    sa.ForeignKeyConstraint(['tenant_id', 'user_id'], ['users.tenant_id', 'users.id'], name='fk_user_connections_user_id_users', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_user_connections_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_connections')),
    sa.UniqueConstraint('tenant_id', 'id', name=op.f('uq_user_connections_tenant_id_id'))
    )
    op.create_index(op.f('ix_user_connections_tenant_id'), 'user_connections', ['tenant_id'], unique=False)
    op.create_index('uq_user_connections_user_provider_live', 'user_connections', ['user_id', 'provider'], unique=True, postgresql_where=sa.text("status <> 'revoked'"))

    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_rls(table)
        op.drop_table(table)
