"""Single sign-on for internal Users (Epic 14, FR-77, AD-30). Migration 20261003_201_02.

* ``identity_providers`` (tenant, RLS): one OIDC or SAML IdP; the client secret is Fernet-sealed (purpose
  ``sso``) and never returned or logged.
* ``identity_provider_group_roles`` (tenant, RLS): optional group -> Role mapping applied at JIT creation
only.
* ``user_identities`` (tenant, RLS): the ``(idp, subject)`` -> User link.
* ``sso_login_states`` (platform, no RLS): the single-use login state (hash of the ``state`` / RelayState
  token -> tenant + IdP), 10-minute TTL. No PII.

Epic 50 (FR-250..FR-252, AD-48; migration 20261007_314_01_client_idp): ``identity_providers`` gains
``audience`` (``internal`` | ``client``), ``client_id`` (required iff ``audience='client'``, CHECK) and
``jit_default_permissions`` (Client Portal permissions given to JIT Client Users). AD-48 ``allowed_domains``
is the existing ``email_domains``. New (tenant RLS + internal-only org policy): ``client_user_identities``
(the ``(idp, subject)`` -> Client User link), ``client_sso_settings`` (per-Client enforcement / approval-link
opt-out) and ``client_sso_exemptions`` (break-glass Client Users).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

IDP_KINDS = ("oidc", "saml")
IDP_STATUSES = ("draft", "active", "disabled")
ENFORCE_MODES = ("none", "all", "roles")
IDP_AUDIENCES = ("internal", "client")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class IdentityProvider(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "identity_providers"
    __audit_exclude__ = ("secret",)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "name"),
        CheckConstraint(_in("kind", IDP_KINDS), name="kind"),
        CheckConstraint(_in("status", IDP_STATUSES), name="status"),
        CheckConstraint(_in("enforce_for", ENFORCE_MODES), name="enforce_for"),
        # Epic 50 (AD-48): a client IdP serves exactly one Client; an internal IdP none.
        CheckConstraint(_in("audience", IDP_AUDIENCES), name="audience"),
        CheckConstraint("(audience = 'client') = (client_id IS NOT NULL)", name="client_audience"),
        _tenant_fk("client_id", "clients", "fk_identity_providers_client_id_clients"),
        Index("ix_identity_providers_client_id", "client_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'draft'"))
    email_domains: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    # Fernet token (purpose ``sso``): the OIDC client secret. Never returned by the API.
    secret: Mapped[str | None] = mapped_column(Text(), nullable=True)
    enforce_for: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'none'"))
    enforced_role_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    jit_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    jit_default_role_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("roles.id", ondelete="SET NULL"), nullable=True
    )
    group_claim: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mfa_trusted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Epic 50 (AD-48).
    audience: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'internal'"))
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    jit_default_permissions: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )


class IdentityProviderGroupRole(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "identity_provider_group_roles"
    __table_args__ = (
        UniqueConstraint("idp_id", "group_value", "role_id"),
        _tenant_fk(
            "idp_id",
            "identity_providers",
            "fk_identity_provider_group_roles_idp_id_identity_providers",
            "CASCADE",
        ),
        _tenant_fk("role_id", "roles", "fk_identity_provider_group_roles_role_id_roles", "CASCADE"),
    )

    idp_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    group_value: Mapped[str] = mapped_column(String(200), nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class UserIdentity(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "user_identities"
    __pii_fields__ = ("email_at_link",)
    __audit_exclude__ = ("last_login_at",)
    __table_args__ = (
        UniqueConstraint("idp_id", "subject"),
        UniqueConstraint("idp_id", "user_id"),
        _tenant_fk("idp_id", "identity_providers", "fk_user_identities_idp_id_identity_providers"),
        _tenant_fk("user_id", "users", "fk_user_identities_user_id_users", "CASCADE"),
        Index("ix_user_identities_user_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idp_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email_at_link: Mapped[str | None] = mapped_column(String(320), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SsoLoginState(Base):
    """Platform table (``core.tenancy.PLATFORM_TABLES``): login state hash -> tenant + IdP. No PII."""

    __tablename__ = "sso_login_states"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    idp_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # OIDC nonce, or the SAML AuthnRequest ID (checked against ``InResponseTo``).
    nonce: Mapped[str] = mapped_column(String(255), nullable=False)
    # Fernet token (purpose ``sso``) of the PKCE verifier (OIDC only).
    pkce_verifier: Mapped[str | None] = mapped_column(Text(), nullable=True)
    relay_to: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ================================================================ Epic 50: Client Portal SSO
class ClientUserIdentity(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """The ``(client IdP, subject)`` -> Client User link. Erased with the Client User
    (``sso.client_identities``)."""

    __tablename__ = "client_user_identities"
    __pii_fields__ = ("email_at_link",)
    __audit_exclude__ = ("last_login_at",)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("idp_id", "subject"),
        UniqueConstraint("idp_id", "client_user_id"),
        _tenant_fk("idp_id", "identity_providers", "fk_client_user_identities_idp_id_identity_providers"),
        _tenant_fk(
            "client_user_id",
            "client_users",
            "fk_client_user_identities_client_user_id_client_users",
            "CASCADE",
        ),
        Index("ix_client_user_identities_client_user_id", "client_user_id"),
    )

    client_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idp_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email_at_link: Mapped[str | None] = mapped_column(String(320), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClientSsoSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """Per-Client SSO enforcement (FR-252). No row = defaults (not required, approval links allowed)."""

    __tablename__ = "client_sso_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "client_id"),
        _tenant_fk("client_id", "clients", "fk_client_sso_settings_client_id_clients", "CASCADE"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sso_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    approval_links_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ClientSsoExemption(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """A break-glass Client User: keeps magic link + code (+ mandatory TOTP) while SSO is required."""

    __tablename__ = "client_sso_exemptions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("client_user_id"),
        _tenant_fk(
            "client_user_id",
            "client_users",
            "fk_client_sso_exemptions_client_user_id_client_users",
            "CASCADE",
        ),
    )

    client_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
