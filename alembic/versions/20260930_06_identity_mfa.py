"""Identity MFA (story 1.4): encrypted TOTP secret + last used step on users/platform_operators,
``sessions.mfa_enrollment_required``, ``mfa_challenges`` (platform table) and ``mfa_recovery_codes``
(mixed tenant/platform RLS).

Revision ID: 20260930_06
Revises: 20260930_05
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import enable_mixed_rls

revision: str = "20260930_06"
down_revision: str | None = "20260930_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRINCIPALS = "principal_type IN ('user', 'platform')"


def upgrade() -> None:
    for table in ("users", "platform_operators"):
        # The placeholder column was never written (MFA did not exist before this story).
        op.drop_column(table, "mfa_secret")
        op.add_column(table, sa.Column("mfa_secret_encrypted", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("mfa_last_step", sa.BigInteger(), nullable=True))

    op.add_column(
        "sessions",
        sa.Column("mfa_enrollment_required", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    op.create_table(
        "mfa_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("principal_type", sa.String(length=16), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(PRINCIPALS, name=op.f("ck_mfa_challenges_principal_type")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mfa_challenges")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_mfa_challenges_token_hash")),
    )
    op.create_index("ix_mfa_challenges_principal", "mfa_challenges", ["principal_type", "principal_id"])

    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("principal_type", sa.String(length=16), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(PRINCIPALS, name=op.f("ck_mfa_recovery_codes_principal_type")),
        sa.CheckConstraint(
            "(principal_type = 'user') = (tenant_id IS NOT NULL)",
            name=op.f("ck_mfa_recovery_codes_tenant_matches_principal"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_mfa_recovery_codes_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mfa_recovery_codes")),
    )
    op.create_index(
        "ix_mfa_recovery_codes_principal", "mfa_recovery_codes", ["principal_type", "principal_id"]
    )
    enable_mixed_rls("mfa_recovery_codes")


def downgrade() -> None:
    op.drop_table("mfa_recovery_codes")
    op.drop_index("ix_mfa_challenges_principal", table_name="mfa_challenges")
    op.drop_table("mfa_challenges")
    op.drop_column("sessions", "mfa_enrollment_required")
    for table in ("users", "platform_operators"):
        op.drop_column(table, "mfa_last_step")
        op.drop_column(table, "mfa_secret_encrypted")
        op.add_column(table, sa.Column("mfa_secret", sa.String(length=255), nullable=True))
