"""Phase 2 Epic 14 (FR-76, AD-30): Legal Entity admin deltas and the break-glass flag.

* ``legal_entity_bank_accounts``: ``branch``, ``account_name``, ``archived_at``; the default account is now
one
  per ``(legal_entity_id, currency)`` among non-archived accounts (NULL currency = "any").
* ``legal_entities``: ``deactivation_reason``, ``deactivated_at``.
* ``users.break_glass``: Tenant Admins allowed to sign in with a password while SSO is enforced.

Revision ID: 20261003_201_01_le_bank_accts
Revises: 20261002_200_07
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261003_201_01_le_bank_accts"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_access",)
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("legal_entity_bank_accounts", sa.Column("branch", sa.String(length=200), nullable=True))
    op.add_column(
        "legal_entity_bank_accounts", sa.Column("account_name", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "legal_entity_bank_accounts", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_index("uq_legal_entity_bank_accounts_default", table_name="legal_entity_bank_accounts")
    op.create_index(
        "uq_legal_entity_bank_accounts_default",
        "legal_entity_bank_accounts",
        ["legal_entity_id", sa.text("coalesce(currency, '')")],
        unique=True,
        postgresql_where=sa.text("is_default AND archived_at IS NULL"),
    )
    op.add_column("legal_entities", sa.Column("deactivation_reason", sa.Text(), nullable=True))
    op.add_column("legal_entities", sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users", sa.Column("break_glass", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("users", "break_glass")
    op.drop_column("legal_entities", "deactivated_at")
    op.drop_column("legal_entities", "deactivation_reason")
    op.drop_index("uq_legal_entity_bank_accounts_default", table_name="legal_entity_bank_accounts")
    # Keep one default per entity before restoring the original index.
    op.execute(
        "UPDATE legal_entity_bank_accounts b SET is_default = false WHERE is_default AND EXISTS ("
        "SELECT 1 FROM legal_entity_bank_accounts o WHERE o.legal_entity_id = b.legal_entity_id "
        "AND o.is_default AND o.id < b.id)"
    )
    op.create_index(
        "uq_legal_entity_bank_accounts_default",
        "legal_entity_bank_accounts",
        ["legal_entity_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.drop_column("legal_entity_bank_accounts", "archived_at")
    op.drop_column("legal_entity_bank_accounts", "account_name")
    op.drop_column("legal_entity_bank_accounts", "branch")
