# ruff: noqa: E501
"""Phase 5 foundation (Epic 66, Story 66.6, spec 0.10): drop the legacy secret columns (now in the vault).

``provider_connector_configs.credentials`` / ``webhook_secret`` and ``user_connections.access_token_enc`` /
``refresh_token_enc``. Downgrade re-adds them empty and nullable (500_05's downgrade refills them from the vault).

Revision ID: 20261009_500_06_p5_vault_drop
Revises: 20261009_500_05_p5_vault_move
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261009_500_06_p5_vault_drop"
down_revision: str | tuple[str, ...] | None = "20261009_500_05_p5_vault_move"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("provider_connector_configs", "credentials")
    op.drop_column("provider_connector_configs", "webhook_secret")
    op.drop_column("user_connections", "access_token_enc")
    op.drop_column("user_connections", "refresh_token_enc")


def downgrade() -> None:
    op.add_column("user_connections", sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=True))
    op.add_column("user_connections", sa.Column("access_token_enc", sa.LargeBinary(), nullable=True))
    op.add_column("provider_connector_configs", sa.Column("webhook_secret", sa.Text(), nullable=True))
    op.add_column("provider_connector_configs", sa.Column("credentials", sa.Text(), nullable=True))
