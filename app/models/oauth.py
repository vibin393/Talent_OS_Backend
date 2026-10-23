# ruff: file-ignore[line-too-long]
"""Apps, Installations and the OAuth 2.1 token store (Epic 66, AD-75/AD-76; migration ``20261009_500_02_p5_oauth``).

Platform tables (no tenant personal data; ``core.tenancy.PLATFORM_TABLES``): ``partner_orgs`` (Epic 74 ALTERs it),
``apps`` (runtime status only; listings and reviews are Epic 69's), ``app_secrets`` (sha256 of ``tos_cs_`` secrets)
and ``oauth_token_lookups`` (token hash -> tenant, like ``api_key_lookups``).

Tenant tables (RLS + internal-only org RLS): ``app_installations`` (Apps **and** connectors, audited),
``app_user_authorizations`` (delegated authorisations, audited), ``app_grants`` (immutable consent history, audited),
``oauth_tokens`` (opaque tokens, only sha256 stored; lifecycle audited explicitly) and ``oauth_auth_codes``.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

PARTNER_STATUSES = ("active", "suspended", "closed")
PARTNER_VERIFICATION_STATUSES = ("unverified", "pending", "verified", "rejected", "expired")
PUBLISHER_KINDS = ("platform", "partner", "tenant")
CLIENT_TYPES = ("confidential", "public")
GRANT_TYPES = ("authorization_code", "client_credentials", "refresh_token")
RESOURCES = ("api", "mcp")
APP_STATUSES = ("development", "published", "suspended", "retired")
CREATED_BY_KINDS = ("tenant_user", "developer", "platform")
INSTALLATION_KINDS = ("app", "connector")
INSTALLATION_STATUSES = (
    "pending_consent", "pending_approval", "active", "degraded", "reconsent_required", "suspended", "uninstalled",
)  # fmt: skip
HEALTH_STATUSES = ("unknown", "ok", "degraded", "failing")
TOKEN_KINDS = ("access", "refresh")
LOOKUP_KINDS = ("access", "refresh", "code")
REVOKE_REASONS = (
    "uninstalled", "suspended", "kill_switch", "user_deactivated", "grant_reduced", "reuse_detected", "revoked",
    "rotated_secret",
)  # fmt: skip


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _str_array(length: int, default: str = "{}") -> Any:
    return mapped_column(ARRAY(String(length)), nullable=False, server_default=text(f"'{default}'"))


# ================================================================ platform tables
class PartnerOrg(UUIDPk, Timestamps, Base):
    __tablename__ = "partner_orgs"
    __table_args__ = (
        UniqueConstraint("slug"),
        CheckConstraint(_in("status", PARTNER_STATUSES), name="status"),
        CheckConstraint(
            _in("verification_status", PARTNER_VERIFICATION_STATUSES), name="verification_status"
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    verification_status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'unverified'")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 74 (508_01, spec A2): the Verified Partner profile (CHECK ``ck_partner_orgs_tax_id_kind`` in the migration).
    legal_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    tax_id_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    website: Mapped[str | None] = mapped_column(Text(), nullable=True)
    support_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    support_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    agreement_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    agreement_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class App(UUIDPk, Timestamps, Base):
    __tablename__ = "apps"
    __table_args__ = (
        UniqueConstraint("client_id"),
        UniqueConstraint("slug"),
        CheckConstraint(_in("publisher_kind", PUBLISHER_KINDS), name="publisher_kind"),
        CheckConstraint(_in("client_type", CLIENT_TYPES), name="client_type"),
        CheckConstraint(_in("status", APP_STATUSES), name="status"),
        CheckConstraint(_in("created_by_kind", CREATED_BY_KINDS), name="created_by_kind"),
        CheckConstraint("(publisher_kind = 'tenant') = (owner_tenant_id IS NOT NULL)", name="tenant_owner"),
        CheckConstraint("(publisher_kind = 'partner') = (partner_org_id IS NOT NULL)", name="partner_owner"),
        CheckConstraint(
            "client_type = 'confidential' OR NOT ('client_credentials' = ANY(grant_types))",
            name="cc_confidential",
        ),
        CheckConstraint(
            "'mcp' <> ALL(resources) OR 'authorization_code' = ANY(grant_types)", name="mcp_delegated"
        ),
        CheckConstraint(
            "grant_types <@ ARRAY['authorization_code', 'client_credentials', 'refresh_token']::varchar[]",
            name="grant_types",
        ),
        CheckConstraint("resources <@ ARRAY['api', 'mcp']::varchar[]", name="resources"),
        Index("ix_apps_owner_tenant_id", "owner_tenant_id"),
        Index("ix_apps_partner_org_id", "partner_org_id"),
    )

    client_id: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    publisher_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    partner_org_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("partner_orgs.id", ondelete="RESTRICT"), nullable=True
    )
    owner_tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_type: Mapped[str] = mapped_column(String(12), nullable=False)
    grant_types: Mapped[list[str]] = mapped_column(ARRAY(String(30)), nullable=False)
    redirect_uris: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'")
    )
    resources: Mapped[list[str]] = _str_array(10, "{api}")
    requested_scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    scope_reasons: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    manifest_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'development'"))
    suspended_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rate_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    logo_file_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    homepage_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    privacy_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    dpa_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    support_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    support_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    created_by_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class AppSecret(UUIDPk, Timestamps, Base):
    __tablename__ = "app_secrets"
    __table_args__ = (UniqueConstraint("secret_hash"), Index("ix_app_secrets_app_id", "app_id"))

    app_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), nullable=False
    )
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(4), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class OAuthTokenLookup(Base):
    """Platform table: ``sha256(token) -> (tenant, token id, kind)``; purged hourly after expiry (no PII)."""

    __tablename__ = "oauth_token_lookups"
    __table_args__ = (
        CheckConstraint(_in("kind", LOOKUP_KINDS), name="kind"),
        Index("ix_oauth_token_lookups_expires_at", "expires_at"),
    )

    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ================================================================ tenant tables
class AppInstallation(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "app_installations"
    __audit_exclude__ = ("config", "health_checked_at")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("granted_by", "users", "fk_app_installations_granted_by_users"),
        _tenant_fk("accountable_user_id", "users", "fk_app_installations_accountable_user_id_users"),
        _tenant_fk("uninstalled_by", "users", "fk_app_installations_uninstalled_by_users"),
        CheckConstraint(_in("kind", INSTALLATION_KINDS), name="kind"),
        CheckConstraint(_in("status", INSTALLATION_STATUSES), name="status"),
        CheckConstraint(_in("health_status", HEALTH_STATUSES), name="health_status"),
        CheckConstraint("(kind = 'app') = (app_id IS NOT NULL)", name="app_ref"),
        CheckConstraint("(kind = 'connector') = (connector_key IS NOT NULL)", name="connector_ref"),
        Index(
            "uq_installations_app",
            "tenant_id",
            "app_id",
            unique=True,
            postgresql_where=text("kind = 'app' AND status <> 'uninstalled'"),
        ),
        Index(
            "uq_installations_connector",
            "tenant_id",
            "connector_key",
            "instance_no",
            unique=True,
            postgresql_where=text("kind = 'connector' AND status <> 'uninstalled'"),
        ),
        Index("ix_app_installations_tenant_id_status", "tenant_id", "status"),
        Index("ix_app_installations_app_id", "app_id"),
    )

    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    app_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("apps.id", ondelete="RESTRICT"), nullable=True
    )
    connector_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    instance_no: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    manifest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pending_manifest_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending_consent'"))
    granted_scopes: Mapped[list[str]] = _str_array(64)
    sensitive_purposes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accountable_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    legal_entity_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    rate_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    health_status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'unknown'"))
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    uninstalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uninstalled_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class AppUserAuthorization(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "app_user_authorizations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(
            "installation_id",
            "app_installations",
            "fk_app_user_authorizations_installation_id_inst",
            "CASCADE",
        ),
        _tenant_fk("user_id", "users", "fk_app_user_authorizations_user_id_users", "CASCADE"),
        CheckConstraint(_in("resource", RESOURCES), name="resource"),
        Index(
            "uq_app_user_authorizations_live",
            "tenant_id",
            "installation_id",
            "user_id",
            "resource",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_app_user_authorizations_user_id", "user_id"),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    resource: Mapped[str] = mapped_column(String(5), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)


class OAuthToken(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "oauth_tokens"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("token_hash"),
        _tenant_fk("installation_id", "app_installations", "fk_oauth_tokens_installation_id_inst", "CASCADE"),
        _tenant_fk("user_id", "users", "fk_oauth_tokens_user_id_users", "CASCADE"),
        _tenant_fk(
            "user_authorization_id",
            "app_user_authorizations",
            "fk_oauth_tokens_user_authorization_id_auth",
            "CASCADE",
        ),
        CheckConstraint(_in("kind", TOKEN_KINDS), name="kind"),
        CheckConstraint(_in("grant_type", GRANT_TYPES), name="grant_type"),
        CheckConstraint(_in("resource", RESOURCES), name="resource"),
        CheckConstraint(
            f"revoked_reason IS NULL OR {_in('revoked_reason', REVOKE_REASONS)}", name="revoked_reason"
        ),
        CheckConstraint("kind = 'access' OR user_id IS NOT NULL", name="refresh_delegated"),
        CheckConstraint("resource = 'api' OR user_id IS NOT NULL", name="mcp_delegated"),
        Index("ix_oauth_tokens_family_id", "family_id"),
        Index("ix_oauth_tokens_installation_id_revoked_at", "installation_id", "revoked_at"),
        Index("ix_oauth_tokens_user_id", "user_id"),
        Index("ix_oauth_tokens_expires_at", "expires_at"),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    grant_type: Mapped[str] = mapped_column(String(20), nullable=False)
    resource: Mapped[str] = mapped_column(String(5), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    user_authorization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    issued_from_code_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthAuthCode(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "oauth_auth_codes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("code_hash"),
        _tenant_fk(
            "installation_id", "app_installations", "fk_oauth_auth_codes_installation_id_inst", "CASCADE"
        ),
        _tenant_fk("user_id", "users", "fk_oauth_auth_codes_user_id_users", "CASCADE"),
        CheckConstraint(_in("resource", RESOURCES), name="resource"),
        CheckConstraint("code_challenge_method = 'S256'", name="pkce_s256"),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    code_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text(), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    resource: Mapped[str] = mapped_column(String(5), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(
        String(5), nullable=False, server_default=text("'S256'")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AppGrant(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "app_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("installation_id", "app_installations", "fk_app_grants_installation_id_inst", "CASCADE"),
        _tenant_fk("granted_by", "users", "fk_app_grants_granted_by_users"),
        Index(
            "uq_app_grants_current",
            "tenant_id",
            "installation_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    sensitive_purposes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    data_flows: Mapped[list[Any] | dict[str, Any]] = mapped_column(JSONB, nullable=False)
    granted_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


__all__ = [
    "App",
    "AppGrant",
    "AppInstallation",
    "AppSecret",
    "AppUserAuthorization",
    "OAuthAuthCode",
    "OAuthToken",
    "OAuthTokenLookup",
    "PartnerOrg",
]
