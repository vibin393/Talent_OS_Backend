"""Phase 2 foundation (Epic 13, stories 13.4 / 13.5): external principals and organization isolation.

* ``vendors`` (contract hub; needed by the ``vendor_users`` FK), ``client_users``, ``client_user_scopes``,
  ``vendor_users``, ``org_grants`` -- tenant RLS + the restrictive ``org_isolation`` policy (AD-26).
* Principal CHECK widening: ``sessions`` / ``mfa_challenges`` / ``mfa_recovery_codes`` accept ``client_user``
  and ``vendor_user``; recovery codes of those principals carry a tenant.

Revision ID: 20261002_200_03
Revises: 20261002_200_02
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261002_200_03"
down_revision: str | tuple[str, ...] | None = "20261002_200_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("vendors", "client_users", "client_user_scopes", "vendor_users", "org_grants")

P1_SESSION_TYPES = ("user", "candidate", "platform", "support")
P2_SESSION_TYPES = (*P1_SESSION_TYPES, "client_user", "vendor_user")
P1_MFA_TYPES = ("user", "platform")
P2_MFA_TYPES = (*P1_MFA_TYPES, "client_user", "vendor_user")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    op.create_table(
        "vendors",
        sa.Column("human_id", sa.String(length=20), nullable=False),
        sa.Column("legal_name", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=12), server_default=sa.text("'prospective'"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('prospective', 'onboarding', 'active', 'suspended', 'inactive')",
            name=op.f("ck_vendors_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_vendors_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendors")),
        sa.UniqueConstraint("tenant_id", "human_id", name=op.f("uq_vendors_tenant_id_human_id")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_vendors_tenant_id_id")),
    )
    op.create_index(op.f("ix_vendors_tenant_id"), "vendors", ["tenant_id"], unique=False)
    op.create_table(
        "client_users",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'invited'"), nullable=False),
        sa.Column(
            "portal_permissions",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("restricted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("mfa_enforced", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("mfa_secret", sa.Text(), nullable=True),
        sa.Column("mfa_enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sign_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invited_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'revoked')", name=op.f("ck_client_users_status")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_client_users_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_client_users_contact_id_contacts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "invited_by"],
            ["users.tenant_id", "users.id"],
            name="fk_client_users_invited_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_client_users_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_users")),
        sa.UniqueConstraint("tenant_id", "contact_id", name=op.f("uq_client_users_tenant_id_contact_id")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_client_users_tenant_id_id")),
    )
    op.create_index("ix_client_users_client_id", "client_users", ["client_id"], unique=False)
    op.create_index(op.f("ix_client_users_tenant_id"), "client_users", ["tenant_id"], unique=False)
    op.create_table(
        "client_user_scopes",
        sa.Column("client_user_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("subject_kind", sa.String(length=10), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "subject_kind IN ('demand', 'placement')", name=op.f("ck_client_user_scopes_subject_kind")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_user_id"],
            ["client_users.tenant_id", "client_users.id"],
            name="fk_client_user_scopes_client_user_id_client_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_client_user_scopes_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_user_scopes")),
        sa.UniqueConstraint(
            "client_user_id",
            "subject_kind",
            "subject_id",
            name=op.f("uq_client_user_scopes_client_user_id_subject_kind_subject_id"),
        ),
    )
    op.create_index(
        op.f("ix_client_user_scopes_tenant_id"), "client_user_scopes", ["tenant_id"], unique=False
    )
    op.create_table(
        "vendor_users",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=12), server_default=sa.text("'invited'"), nullable=False),
        sa.Column("is_vendor_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "portal_permissions",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("mfa_secret", sa.Text(), nullable=True),
        sa.Column("mfa_enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sign_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'deactivated')", name=op.f("ck_vendor_users_status")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "vendor_id"],
            ["vendors.tenant_id", "vendors.id"],
            name="fk_vendor_users_vendor_id_vendors",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_vendor_users_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_users")),
        sa.UniqueConstraint("tenant_id", "email", name=op.f("uq_vendor_users_tenant_id_email")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_vendor_users_tenant_id_id")),
    )
    op.create_index(op.f("ix_vendor_users_tenant_id"), "vendor_users", ["tenant_id"], unique=False)
    op.create_index("ix_vendor_users_vendor_id", "vendor_users", ["vendor_id"], unique=False)
    op.create_table(
        "org_grants",
        sa.Column("org_kind", sa.String(length=10), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("subject_kind", sa.String(length=40), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("granted_by_kind", sa.String(length=20), nullable=False),
        sa.Column("granted_by_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("org_kind IN ('client', 'vendor', 'worker')", name=op.f("ck_org_grants_org_kind")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_org_grants_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_grants")),
        sa.UniqueConstraint(
            "tenant_id",
            "org_kind",
            "org_id",
            "subject_kind",
            "subject_id",
            name=op.f("uq_org_grants_tenant_id_org_kind_org_id_subject_kind_subject_id"),
        ),
    )
    op.create_index("ix_org_grants_subject", "org_grants", ["subject_kind", "subject_id"], unique=False)
    op.create_index(op.f("ix_org_grants_tenant_id"), "org_grants", ["tenant_id"], unique=False)
    for table in TABLES:
        enable_rls(table)
    enable_org_rls("vendors", vendor_col="id")
    enable_org_rls("client_users", client_col="client_id")
    enable_org_rls(
        "client_user_scopes",
        client_col="client_id",
        inherit_from=InheritRef("client_users", "client_user_id", ("client_id",)),
    )
    enable_org_rls("vendor_users", vendor_col="vendor_id")
    enable_org_rls("org_grants", self_org=True)

    _replace_check("sessions", "ck_sessions_principal_type", _in("principal_type", P2_SESSION_TYPES))
    _replace_check("mfa_challenges", "ck_mfa_challenges_principal_type", _in("principal_type", P2_MFA_TYPES))
    _replace_check(
        "mfa_recovery_codes", "ck_mfa_recovery_codes_principal_type", _in("principal_type", P2_MFA_TYPES)
    )
    _replace_check(
        "mfa_recovery_codes",
        "ck_mfa_recovery_codes_tenant_matches_principal",
        "(principal_type IN ('user', 'client_user', 'vendor_user')) = (tenant_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM sessions WHERE principal_type IN ('client_user', 'vendor_user')")
    op.execute("DELETE FROM mfa_challenges WHERE principal_type IN ('client_user', 'vendor_user')")
    op.execute("DELETE FROM mfa_recovery_codes WHERE principal_type IN ('client_user', 'vendor_user')")
    _replace_check(
        "mfa_recovery_codes",
        "ck_mfa_recovery_codes_tenant_matches_principal",
        "(principal_type = 'user') = (tenant_id IS NOT NULL)",
    )
    _replace_check(
        "mfa_recovery_codes", "ck_mfa_recovery_codes_principal_type", _in("principal_type", P1_MFA_TYPES)
    )
    _replace_check("mfa_challenges", "ck_mfa_challenges_principal_type", _in("principal_type", P1_MFA_TYPES))
    _replace_check("sessions", "ck_sessions_principal_type", _in("principal_type", P1_SESSION_TYPES))
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
