# ruff: noqa: E501
"""Phase 5 foundation (Epic 66, stories 66.2 / 66.10): Apps, Installations, consent history and the OAuth 2.1 token
store (spec 0.2).

Platform (``core.tenancy.PLATFORM_TABLES``; no tenant personal data; default ``talent_app`` DML grants):
``partner_orgs``, ``apps``, ``app_secrets``, ``oauth_token_lookups``.
Tenant (RLS + internal-only org RLS): ``app_installations``, ``app_grants``, ``app_user_authorizations``,
``oauth_tokens``, ``oauth_auth_codes``, ``ecosystem_settings``.

Revision ID: 20261009_500_02_p5_oauth
Revises: 20261009_500_01_p5_checks
Create Date: 2026-10-09
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261009_500_02_p5_oauth"
down_revision: str | tuple[str, ...] | None = "20261009_500_01_p5_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "app_installations", "app_user_authorizations", "oauth_tokens", "oauth_auth_codes", "app_grants",
    "ecosystem_settings",
)  # fmt: skip
INSTALLATION_STATUSES = (
    "pending_consent", "pending_approval", "active", "degraded", "reconsent_required", "suspended", "uninstalled",
)  # fmt: skip
REVOKE_REASONS = (
    "uninstalled", "suspended", "kill_switch", "user_deactivated", "grant_reduced", "reuse_detected", "revoked",
    "rotated_secret",
)  # fmt: skip
APP_POLICY_DEFAULT = (
    """'{"publisher_kinds": ["platform", "partner"], "private_apps": true, "user_self_authorize": true, """
    """"sensitive_approval_chain_id": null}'::jsonb"""
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Uuid(), nullable=nullable)


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _platform_base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
    ]


def _tenant_base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _arr(name: str, length: int, default: str | None = "{}", nullable: bool = False) -> sa.Column:
    kw: dict[str, Any] = {"nullable": nullable}
    if default is not None:
        kw["server_default"] = sa.text(f"'{default}'")
    return sa.Column(name, postgresql.ARRAY(sa.String(length)), **kw)


def _jsonb(name: str, default: str | None = "{}") -> sa.Column:
    if default is None:
        return sa.Column(name, postgresql.JSONB(), nullable=False)
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def upgrade() -> None:
    # ---------------------------------------------------------------- platform
    t = "partner_orgs"
    op.create_table(
        t,
        *_platform_base(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", postgresql.CITEXT(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        sa.Column(
            "verification_status", sa.String(12), server_default=sa.text("'unverified'"), nullable=False
        ),
        _ts("verified_at"),
        _uuid("verified_by"),
        _ts("verification_expires_at"),
        sa.UniqueConstraint("slug", name="uq_partner_orgs_slug"),
        _ck(t, "status", "status IN ('active', 'suspended', 'closed')"),
        _ck(
            t,
            "verification_status",
            "verification_status IN ('unverified', 'pending', 'verified', 'rejected', 'expired')",
        ),
    )

    t = "apps"
    op.create_table(
        t,
        *_platform_base(t),
        sa.Column("client_id", sa.String(40), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", postgresql.CITEXT(), nullable=False),
        sa.Column("publisher_kind", sa.String(10), nullable=False),
        _uuid("partner_org_id"),
        _uuid("owner_tenant_id"),
        sa.Column("client_type", sa.String(12), nullable=False),
        _arr("grant_types", 30, default=None),
        sa.Column(
            "redirect_uris", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        _arr("resources", 10, default="{api}"),
        _arr("requested_scopes", 64, default=None),
        _jsonb("scope_reasons"),
        _jsonb("manifest", default=None),
        sa.Column("manifest_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'development'"), nullable=False),
        sa.Column("suspended_reason", sa.String(500), nullable=True),
        sa.Column("rate_tier", sa.String(20), nullable=True),
        sa.Column("logo_file_key", sa.String(300), nullable=True),
        sa.Column("homepage_url", sa.Text(), nullable=True),
        sa.Column("privacy_url", sa.Text(), nullable=True),
        sa.Column("dpa_url", sa.Text(), nullable=True),
        sa.Column("support_url", sa.Text(), nullable=True),
        sa.Column("support_email", postgresql.CITEXT(), nullable=True),
        sa.Column("created_by_kind", sa.String(12), nullable=False),
        _uuid("created_by_id"),
        sa.UniqueConstraint("client_id", name="uq_apps_client_id"),
        sa.UniqueConstraint("slug", name="uq_apps_slug"),
        sa.ForeignKeyConstraint(
            ["partner_org_id"],
            ["partner_orgs.id"],
            name="fk_apps_partner_org_id_partner_orgs",
            ondelete="RESTRICT",
        ),
        _ck(t, "publisher_kind", "publisher_kind IN ('platform', 'partner', 'tenant')"),
        _ck(t, "client_type", "client_type IN ('confidential', 'public')"),
        _ck(t, "status", "status IN ('development', 'published', 'suspended', 'retired')"),
        _ck(t, "created_by_kind", "created_by_kind IN ('tenant_user', 'developer', 'platform')"),
        _ck(t, "tenant_owner", "(publisher_kind = 'tenant') = (owner_tenant_id IS NOT NULL)"),
        _ck(t, "partner_owner", "(publisher_kind = 'partner') = (partner_org_id IS NOT NULL)"),
        _ck(
            t,
            "cc_confidential",
            "client_type = 'confidential' OR NOT ('client_credentials' = ANY(grant_types))",
        ),
        _ck(t, "mcp_delegated", "'mcp' <> ALL(resources) OR 'authorization_code' = ANY(grant_types)"),
        _ck(
            t,
            "grant_types",
            "grant_types <@ ARRAY['authorization_code', 'client_credentials', 'refresh_token']::varchar[]",
        ),
        _ck(t, "resources", "resources <@ ARRAY['api', 'mcp']::varchar[]"),
    )
    op.create_index("ix_apps_owner_tenant_id", t, ["owner_tenant_id"])
    op.create_index("ix_apps_partner_org_id", t, ["partner_org_id"])

    t = "app_secrets"
    op.create_table(
        t,
        *_platform_base(t),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("secret_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.String(4), nullable=False),
        _ts("expires_at"),
        _ts("revoked_at"),
        _ts("last_used_at"),
        _uuid("created_by_id"),
        sa.UniqueConstraint("secret_hash", name="uq_app_secrets_secret_hash"),
        sa.ForeignKeyConstraint(
            ["app_id"], ["apps.id"], name="fk_app_secrets_app_id_apps", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_app_secrets_app_id", t, ["app_id"])

    t = "oauth_token_lookups"
    op.create_table(
        t,
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        _ts("expires_at", nullable=False),
        sa.PrimaryKeyConstraint("token_hash", name="pk_oauth_token_lookups"),
        _ck(t, "kind", "kind IN ('access', 'refresh', 'code')"),
    )
    op.create_index("ix_oauth_token_lookups_expires_at", t, ["expires_at"])

    # ---------------------------------------------------------------- tenant
    t = "app_installations"
    op.create_table(
        t,
        *_tenant_base(t),
        sa.Column("kind", sa.String(10), nullable=False),
        _uuid("app_id"),
        sa.Column("connector_key", sa.String(40), nullable=True),
        sa.Column("instance_no", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("manifest_version", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("pending_manifest_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending_consent'"), nullable=False),
        _arr("granted_scopes", 64),
        _jsonb("sensitive_purposes"),
        _uuid("granted_by"),
        _ts("granted_at"),
        _uuid("accountable_user_id"),
        _jsonb("config"),
        sa.Column(
            "legal_entity_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("rate_tier", sa.String(20), nullable=True),
        sa.Column("health_status", sa.String(10), server_default=sa.text("'unknown'"), nullable=False),
        _ts("health_checked_at"),
        sa.Column("last_error_code", sa.String(60), nullable=True),
        _ts("last_error_at"),
        sa.Column("suspended_reason", sa.String(500), nullable=True),
        _ts("uninstalled_at"),
        _uuid("uninstalled_by"),
        sa.ForeignKeyConstraint(
            ["app_id"], ["apps.id"], name="fk_app_installations_app_id_apps", ondelete="RESTRICT"
        ),
        _fk("granted_by", "users", "fk_app_installations_granted_by_users"),
        _fk("accountable_user_id", "users", "fk_app_installations_accountable_user_id_users"),
        _fk("uninstalled_by", "users", "fk_app_installations_uninstalled_by_users"),
        _ck(t, "kind", "kind IN ('app', 'connector')"),
        _ck(t, "status", _in("status", INSTALLATION_STATUSES)),
        _ck(t, "health_status", "health_status IN ('unknown', 'ok', 'degraded', 'failing')"),
        _ck(t, "app_ref", "(kind = 'app') = (app_id IS NOT NULL)"),
        _ck(t, "connector_ref", "(kind = 'connector') = (connector_key IS NOT NULL)"),
    )
    op.create_index(
        "uq_installations_app", t, ["tenant_id", "app_id"], unique=True,
        postgresql_where=sa.text("kind = 'app' AND status <> 'uninstalled'"),
    )  # fmt: skip
    op.create_index(
        "uq_installations_connector", t, ["tenant_id", "connector_key", "instance_no"], unique=True,
        postgresql_where=sa.text("kind = 'connector' AND status <> 'uninstalled'"),
    )  # fmt: skip
    op.create_index("ix_app_installations_tenant_id_status", t, ["tenant_id", "status"])
    op.create_index("ix_app_installations_app_id", t, ["app_id"])

    t = "app_user_authorizations"
    op.create_table(
        t,
        *_tenant_base(t),
        _uuid("installation_id", nullable=False),
        _uuid("user_id", nullable=False),
        _arr("scopes", 64, default=None),
        sa.Column("resource", sa.String(5), nullable=False),
        _ts("granted_at", nullable=False),
        _ts("revoked_at"),
        sa.Column("revoked_reason", sa.String(40), nullable=True),
        _fk(
            "installation_id",
            "app_installations",
            "fk_app_user_authorizations_installation_id_inst",
            "CASCADE",
        ),
        _fk("user_id", "users", "fk_app_user_authorizations_user_id_users", "CASCADE"),
        _ck(t, "resource", "resource IN ('api', 'mcp')"),
    )
    op.create_index(
        "uq_app_user_authorizations_live", t, ["tenant_id", "installation_id", "user_id", "resource"], unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )  # fmt: skip
    op.create_index("ix_app_user_authorizations_user_id", t, ["user_id"])

    t = "oauth_tokens"
    op.create_table(
        t,
        *_tenant_base(t),
        _uuid("installation_id", nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("grant_type", sa.String(20), nullable=False),
        sa.Column("resource", sa.String(5), nullable=False),
        _uuid("user_id"),
        _uuid("user_authorization_id"),
        _arr("scopes", 64, default=None),
        _uuid("family_id", nullable=False),
        _uuid("parent_id"),
        _uuid("issued_from_code_id"),
        _ts("expires_at", nullable=False),
        _ts("absolute_expires_at"),
        _ts("rotated_at"),
        _ts("revoked_at"),
        sa.Column("revoked_reason", sa.String(40), nullable=True),
        _ts("last_used_at"),
        sa.UniqueConstraint("token_hash", name="uq_oauth_tokens_token_hash"),
        _fk("installation_id", "app_installations", "fk_oauth_tokens_installation_id_inst", "CASCADE"),
        _fk("user_id", "users", "fk_oauth_tokens_user_id_users", "CASCADE"),
        _fk(
            "user_authorization_id",
            "app_user_authorizations",
            "fk_oauth_tokens_user_authorization_id_auth",
            "CASCADE",
        ),
        _ck(t, "kind", "kind IN ('access', 'refresh')"),
        _ck(t, "grant_type", "grant_type IN ('authorization_code', 'client_credentials', 'refresh_token')"),
        _ck(t, "resource", "resource IN ('api', 'mcp')"),
        _ck(t, "revoked_reason", f"revoked_reason IS NULL OR {_in('revoked_reason', REVOKE_REASONS)}"),
        _ck(t, "refresh_delegated", "kind = 'access' OR user_id IS NOT NULL"),
        _ck(t, "mcp_delegated", "resource = 'api' OR user_id IS NOT NULL"),
    )
    op.create_index("ix_oauth_tokens_family_id", t, ["family_id"])
    op.create_index("ix_oauth_tokens_installation_id_revoked_at", t, ["installation_id", "revoked_at"])
    op.create_index("ix_oauth_tokens_user_id", t, ["user_id"])
    op.create_index("ix_oauth_tokens_expires_at", t, ["expires_at"])

    t = "oauth_auth_codes"
    op.create_table(
        t,
        *_tenant_base(t),
        _uuid("installation_id", nullable=False),
        _uuid("user_id", nullable=False),
        sa.Column("code_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        _arr("scopes", 64, default=None),
        sa.Column("resource", sa.String(5), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("code_challenge_method", sa.String(5), server_default=sa.text("'S256'"), nullable=False),
        _ts("expires_at", nullable=False),
        _ts("consumed_at"),
        sa.UniqueConstraint("code_hash", name="uq_oauth_auth_codes_code_hash"),
        _fk("installation_id", "app_installations", "fk_oauth_auth_codes_installation_id_inst", "CASCADE"),
        _fk("user_id", "users", "fk_oauth_auth_codes_user_id_users", "CASCADE"),
        _ck(t, "resource", "resource IN ('api', 'mcp')"),
        _ck(t, "pkce_s256", "code_challenge_method = 'S256'"),
    )

    t = "app_grants"
    op.create_table(
        t,
        *_tenant_base(t),
        _uuid("installation_id", nullable=False),
        sa.Column("manifest_version", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        _arr("scopes", 64, default=None),
        _jsonb("sensitive_purposes"),
        _jsonb("data_flows", default=None),
        _uuid("granted_by", nullable=False),
        _ts("granted_at", nullable=False),
        _ts("superseded_at"),
        _uuid("approval_id"),
        _fk("installation_id", "app_installations", "fk_app_grants_installation_id_inst", "CASCADE"),
        _fk("granted_by", "users", "fk_app_grants_granted_by_users"),
    )
    op.create_index(
        "uq_app_grants_current", t, ["tenant_id", "installation_id"], unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )  # fmt: skip

    t = "ecosystem_settings"
    op.create_table(
        t,
        *_tenant_base(t),
        _ts("kill_switch_at"),
        _uuid("kill_switch_by"),
        sa.Column("kill_switch_reason", sa.String(500), nullable=True),
        _ts("platform_suspended_at"),
        _uuid("platform_suspended_by"),
        sa.Column("platform_suspended_reason", sa.String(500), nullable=True),
        sa.Column("mcp_notice_version", sa.String(20), nullable=True),
        _ts("mcp_enabled_at"),
        _uuid("mcp_enabled_by"),
        _arr("mcp_tool_groups", 40),
        sa.Column(
            "app_policy", postgresql.JSONB(), server_default=sa.text(APP_POLICY_DEFAULT), nullable=False
        ),
        sa.Column("rate_tier", sa.String(20), nullable=True),
        sa.UniqueConstraint("tenant_id", name="uq_ecosystem_settings_tenant_id"),
        _fk("kill_switch_by", "users", "fk_ecosystem_settings_kill_switch_by_users"),
    )

    for table in TENANT_TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
    for table in ("oauth_token_lookups", "app_secrets", "apps", "partner_orgs"):
        op.drop_table(table)
