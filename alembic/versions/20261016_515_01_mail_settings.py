# ruff: noqa: E501
"""Per-tenant outbound email (SMTP) settings: one row per tenant (singleton on ``tenant_id``).

Lets each org send mail from its own SMTP account instead of the shared platform mailer. The SMTP password is
stored Fernet-encrypted (``smtp_password_encrypted``; ``app.core.security`` ``connector`` purpose) and is never
stored or returned in plaintext. Tenant-owned: tenant RLS + internal-only org RLS. Chains onto the current
head so a plain ``alembic upgrade heads`` (and the ``tenant_template`` migrate) applies it last.

Revision ID: 20261016_515_01_mail_settings
Revises: 20261010_514_01_ingest_loops
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261016_515_01_mail_settings"
down_revision: str | tuple[str, ...] | None = "20261010_514_01_ingest_loops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    t = "tenant_mail_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("backend", sa.String(10), server_default=sa.text("'platform'"), nullable=False),
        sa.Column("smtp_address", sa.String(255), nullable=True),
        sa.Column("smtp_port", sa.Integer(), server_default=sa.text("587"), nullable=False),
        sa.Column("smtp_username", sa.String(320), nullable=True),
        sa.Column("smtp_password_encrypted", sa.Text(), nullable=True),
        sa.Column("smtp_use_ssl", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("sender_email", sa.String(320), nullable=True),
        sa.Column("sender_name", sa.String(200), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant_mail_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_mail_settings_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "updated_by"],
            ["users.tenant_id", "users.id"],
            name="fk_tenant_mail_settings_updated_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("backend IN ('platform', 'smtp')", name=op.f("ck_tenant_mail_settings_backend")),
        sa.CheckConstraint("smtp_port BETWEEN 1 AND 65535", name=op.f("ck_tenant_mail_settings_smtp_port")),
    )
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    disable_org_rls("tenant_mail_settings")
    disable_rls("tenant_mail_settings")
    op.drop_table("tenant_mail_settings")
