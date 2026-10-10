"""Phase 2 Epic 14 (FR-78, FR-79, AD-10, AD-26): Client / Vendor User sign-in columns.

Invitation, magic-link and password-reset tokens reuse the platform ``secure_links`` primitive (purposes
``client_sign_in``, ``vendor_invite``, ``vendor_password_reset``); the 6-digit sign-in code is stored
hashed in the link's metadata. This revision adds the per-principal sign-in bookkeeping:

* ``client_users.mfa_last_step`` (TOTP replay guard).
* ``vendor_users``: ``mfa_last_step``, lockout counters (``failed_login_count``, ``first_failed_at``,
  ``locked_until``), ``invited_by_kind`` / ``invited_by_id``, ``password_changed_at``.

Epic 18 (``p2_client_portal``) ``depends_on`` this revision.

Revision ID: 20261003_201_03_external_auth
Revises: 20261003_201_02_sso
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261003_201_03_external_auth"
down_revision: str | tuple[str, ...] | None = "20261003_201_02_sso"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("client_users", sa.Column("mfa_last_step", sa.BigInteger(), nullable=True))
    op.add_column("vendor_users", sa.Column("mfa_last_step", sa.BigInteger(), nullable=True))
    op.add_column(
        "vendor_users",
        sa.Column("failed_login_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("vendor_users", sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("vendor_users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("vendor_users", sa.Column("invited_by_kind", sa.String(length=20), nullable=True))
    op.add_column("vendor_users", sa.Column("invited_by_id", sa.Uuid(), nullable=True))
    op.add_column("vendor_users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for column in (
        "password_changed_at",
        "invited_by_id",
        "invited_by_kind",
        "locked_until",
        "first_failed_at",
        "failed_login_count",
        "mfa_last_step",
    ):
        op.drop_column("vendor_users", column)
    op.drop_column("client_users", "mfa_last_step")
