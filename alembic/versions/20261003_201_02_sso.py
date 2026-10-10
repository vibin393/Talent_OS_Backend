"""Phase 2 Epic 14 (FR-77, AD-30): single sign-on for internal Users.

* ``identity_providers``, ``identity_provider_group_roles``, ``user_identities`` -- tenant RLS plus an
  internal-only org policy (portal principals never see them).
* ``sso_login_states`` -- platform table (no RLS, no PII): login state hash -> tenant + IdP, single use.

Revision ID: 20261003_201_02_sso
Revises: 20261003_201_01_le_bank_accts
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261003_201_02_sso"
down_revision: str | tuple[str, ...] | None = "20261003_201_01_le_bank_accts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("identity_providers", "identity_provider_group_roles", "user_identities")


def _base_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "identity_providers",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'draft'"), nullable=False),
        sa.Column(
            "email_domains",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("secret", sa.Text(), nullable=True),
        sa.Column("enforce_for", sa.String(length=10), server_default=sa.text("'none'"), nullable=False),
        sa.Column(
            "enforced_role_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("jit_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("jit_default_role_id", sa.Uuid(), nullable=True),
        sa.Column("group_claim", sa.String(length=100), nullable=True),
        sa.Column("mfa_trusted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_base_columns(),
        sa.CheckConstraint("kind IN ('oidc', 'saml')", name=op.f("ck_identity_providers_kind")),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'disabled')", name=op.f("ck_identity_providers_status")
        ),
        sa.CheckConstraint(
            "enforce_for IN ('none', 'all', 'roles')", name=op.f("ck_identity_providers_enforce_for")
        ),
        sa.ForeignKeyConstraint(
            ["jit_default_role_id"],
            ["roles.id"],
            name=op.f("fk_identity_providers_jit_default_role_id_roles"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_identity_providers_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_identity_providers_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_providers")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_identity_providers_tenant_id_id")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_identity_providers_tenant_id_name")),
    )
    op.create_index(
        op.f("ix_identity_providers_tenant_id"), "identity_providers", ["tenant_id"], unique=False
    )

    op.create_table(
        "identity_provider_group_roles",
        sa.Column("idp_id", sa.Uuid(), nullable=False),
        sa.Column("group_value", sa.String(length=200), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        *_base_columns(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "idp_id"],
            ["identity_providers.tenant_id", "identity_providers.id"],
            name="fk_identity_provider_group_roles_idp_id_identity_providers",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["roles.tenant_id", "roles.id"],
            name="fk_identity_provider_group_roles_role_id_roles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_identity_provider_group_roles_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_provider_group_roles")),
        sa.UniqueConstraint(
            "idp_id",
            "group_value",
            "role_id",
            name=op.f("uq_identity_provider_group_roles_idp_id_group_value_role_id"),
        ),
    )
    op.create_index(
        op.f("ix_identity_provider_group_roles_tenant_id"),
        "identity_provider_group_roles",
        ["tenant_id"],
        unique=False,
    )

    op.create_table(
        "user_identities",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("idp_id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email_at_link", sa.String(length=320), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        *_base_columns(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "idp_id"],
            ["identity_providers.tenant_id", "identity_providers.id"],
            name="fk_user_identities_idp_id_identity_providers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_user_identities_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_user_identities_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_identities")),
        sa.UniqueConstraint("idp_id", "subject", name=op.f("uq_user_identities_idp_id_subject")),
        sa.UniqueConstraint("idp_id", "user_id", name=op.f("uq_user_identities_idp_id_user_id")),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_identities_tenant_id"), "user_identities", ["tenant_id"], unique=False)

    op.create_table(
        "sso_login_states",
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("idp_id", sa.Uuid(), nullable=False),
        sa.Column("nonce", sa.String(length=255), nullable=False),
        sa.Column("pkce_verifier", sa.Text(), nullable=True),
        sa.Column("relay_to", sa.String(length=500), nullable=True),
        sa.Column("is_test", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("state_hash", name=op.f("pk_sso_login_states")),
    )
    op.create_index(op.f("ix_sso_login_states_tenant_id"), "sso_login_states", ["tenant_id"], unique=False)

    for table in TABLES:
        enable_rls(table)
        enable_org_rls(table)  # internal-only: portal principals see nothing


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
    op.drop_index(op.f("ix_sso_login_states_tenant_id"), table_name="sso_login_states")
    op.drop_table("sso_login_states")
    op.drop_index(op.f("ix_user_identities_tenant_id"), table_name="user_identities")
    op.drop_index("ix_user_identities_user_id", table_name="user_identities")
    op.drop_table("user_identities")
    op.drop_index(
        op.f("ix_identity_provider_group_roles_tenant_id"), table_name="identity_provider_group_roles"
    )
    op.drop_table("identity_provider_group_roles")
    op.drop_index(op.f("ix_identity_providers_tenant_id"), table_name="identity_providers")
    op.drop_table("identity_providers")
