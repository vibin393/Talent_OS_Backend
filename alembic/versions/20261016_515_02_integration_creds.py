# ruff: noqa: E501
"""Per-tenant spoke -> hub integration credentials: ``tenant_integration_credentials`` (platform table).

One row per ``(tenant_id, service)`` holding the Fernet-encrypted plaintext of a Public API key minted for that
tenant's own ATS silo, so the internal service endpoint (``/api/v1/internal/tenant-ats-key``) can return the SAME
key on later calls (Public API keys are stored hash-only). Platform table: NO RLS, no tenant business data, keyed
by the central ``tenant_id`` across tenants (added to ``core.tenancy.PLATFORM_TABLES``; ``talent_app`` DML comes
from the default privileges granted at role provisioning, as for ``api_key_lookups``). Chains onto the current
head so a plain ``alembic upgrade heads`` (and the platform / per-tenant fan-out) applies it last.

Revision ID: 20261016_515_02_integ_creds
Revises: 20261016_515_01_mail_settings
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261016_515_02_integ_creds"
down_revision: str | tuple[str, ...] | None = "20261016_515_01_mail_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_integration_credentials",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("service", sa.String(length=32), server_default=sa.text("'ats'"), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "service", name=op.f("pk_tenant_integration_credentials")),
    )


def downgrade() -> None:
    op.drop_table("tenant_integration_credentials")
