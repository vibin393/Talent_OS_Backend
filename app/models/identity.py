"""Identity tables.

Platform tables (no RLS, see ``app.core.tenancy.PLATFORM_TABLES``): ``tenants``, ``platform_operators``,
``sessions``, ``mfa_challenges``. Mixed (``MIXED_TABLES``): ``mfa_recovery_codes``. Tenant-owned (RLS):
``invitations``, ``users``, ``password_resets``, ``roles``, ``user_roles``, ``business_units``, ``teams``,
``team_members``.

References between tenant-owned rows use composite foreign keys ``(tenant_id, x_id) -> x(tenant_id, id)``:
FK checks bypass RLS, so a plain FK would let a row point at another tenant's role, team or business unit.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

TENANT_STATUSES = ("active", "suspended", "offboarding", "deleted")  # lifecycle: Epic 12 (FR-70)
PRINCIPAL_TYPES = ("user", "candidate", "platform", "support", "client_user", "vendor_user")


class LoginThrottle:
    """Failed sign-in bookkeeping shared by every password principal (users, platform operators).

    See ``identity.service.check_login_attempt``: 5 failures within 15 min lock the row for 15 min.
    """

    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    first_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Tenant(UUIDPk, Timestamps, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended', 'offboarding', 'deleted')", name="status"),
        CheckConstraint("seat_limit > 0", name="seat_limit_positive"),
        CheckConstraint(
            "session_idle_minutes IS NULL OR session_idle_minutes BETWEEN 1 AND 1440", name="session_idle"
        ),
        CheckConstraint(
            "session_absolute_hours IS NULL OR session_absolute_hours BETWEEN 1 AND 720",
            name="session_absolute",
        ),
        # Epic 66 (500_04, AD-85): Sandbox Tenant mode and the Rate Tier.
        CheckConstraint("mode IN ('production', 'sandbox')", name="mode"),
        CheckConstraint(
            "sandbox_owner_kind IS NULL OR sandbox_owner_kind IN ('partner_org', 'tenant', 'platform')",
            name="sandbox_owner_kind",
        ),
        CheckConstraint("(mode = 'sandbox') = (sandbox_owner_kind IS NOT NULL)", name="sandbox_owner"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    seat_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    mfa_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # Tenant overrides of SESSION_IDLE_TIMEOUT_HOURS / SESSION_ABSOLUTE_TIMEOUT_DAYS (NULL = default).
    session_idle_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_absolute_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Lifecycle (Epic 12, migration 20261001_123); changed only by ``tenant_lifecycle_service``.
    status_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    offboard_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    offboard_contact_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 66 (500_04): Sandbox Tenants (synthetic data, Fake adapters only) and the platform Rate Tier.
    mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'production'"))
    sandbox_owner_kind: Mapped[str | None] = mapped_column(String(12), nullable=True)
    sandbox_owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sandbox_last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rate_tier: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'standard'"))


class PlatformOperator(UUIDPk, LoginThrottle, Timestamps, Base):
    __tablename__ = "platform_operators"

    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # TOTP (story 1.4): Fernet-encrypted base32 secret (pending until ``mfa_enabled``), last used time step.
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(Text(), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 69 (503_01): platform roles (``marketplace_reviewer`` 69/74, ``network_moderator`` 75); ``{}`` keeps every P1 power.
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(30)), nullable=False, server_default=text("'{}'"))


class Session(UUIDPk, Base):
    """Opaque session for any principal. Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "principal_type IN ('user', 'candidate', 'platform', 'support', 'client_user', 'vendor_user')",
            name="principal_type",
        ),
        Index("ix_sessions_principal", "principal_type", "principal_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    principal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Lookup hint only (sessions is a platform table); NULL for platform operators.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Issued to a user without MFA while the tenant requires it (story 1.4); see service.authenticate_user.
    mfa_enrollment_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # Support-access sessions (``principal_type='support'``, Epic 12): the approved grant they belong to.
    support_grant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)


class MfaChallenge(UUIDPk, Base):
    """Short-lived "password OK, code pending" state behind the ``tos_mfa`` cookie (platform table, like
    ``sessions``). Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "mfa_challenges"
    __table_args__ = (
        CheckConstraint(
            "principal_type IN ('user', 'platform', 'client_user', 'vendor_user')", name="principal_type"
        ),
        Index("ix_mfa_challenges_principal", "principal_type", "principal_id"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    principal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)  # lookup hint
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MfaRecoveryCode(UUIDPk, Base):
    """One-time MFA recovery code (hash only). Mixed table: user codes carry ``tenant_id`` (RLS), platform
    operator codes have ``tenant_id`` NULL (visible only without tenant context)."""

    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (
        CheckConstraint(
            "principal_type IN ('user', 'platform', 'client_user', 'vendor_user')", name="principal_type"
        ),
        CheckConstraint(
            "(principal_type IN ('user', 'client_user', 'vendor_user')) = (tenant_id IS NOT NULL)",
            name="tenant_matches_principal",
        ),
        Index("ix_mfa_recovery_codes_principal", "principal_type", "principal_id"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=True
    )
    principal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Invitation(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """Single-use invitation into a tenant. Only the SHA-256 of the emailed token is stored."""

    __tablename__ = "invitations"
    __pii_fields__ = ("email", "name")
    __table_args__ = (CheckConstraint("invited_by_type IN ('user', 'platform')", name="invited_by_type"),)

    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)  # suggested; set on acceptance
    # Roles / business unit / teams assigned on acceptance (ids that no longer exist by then are skipped).
    role_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    team_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    invited_by_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class User(UUIDPk, TenantMixin, LoginThrottle, Timestamps, AuditedMixin, Base):
    """Internal-app user of one tenant. Roles via ``user_roles``, teams via ``team_members`` (story 1.5)."""

    __tablename__ = "users"
    __pii_fields__ = ("email", "name")
    # Sign-in bookkeeping is covered by the explicit auth.* audit events.
    __audit_exclude__ = (
        "failed_login_count",
        "first_failed_at",
        "locked_until",
        "last_login_at",
        "mfa_last_step",
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "email"),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "business_unit_id"],
            ["business_units.tenant_id", "business_units.id"],
            name="fk_users_business_unit_id_business_units",
            ondelete="RESTRICT",
        ),
        Index("ix_users_business_unit_id", "business_unit_id"),
    )

    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # TOTP (story 1.4): Fernet-encrypted base32 secret (pending until ``mfa_enabled``), last used time step.
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(Text(), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    # Epic 14 (AD-30, migration 20261003_201_01): may sign in with a password while SSO is enforced.
    break_glass: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class PasswordReset(UUIDPk, TenantMixin, Base):
    """Single-use password reset link. Only the SHA-256 of the emailed token is stored."""

    __tablename__ = "password_resets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ================================================================ RBAC and organization (story 1.5)
class BusinessUnit(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "business_units"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        UniqueConstraint("tenant_id", "id"),
        # Epic 13 (AD-17, migration 20261002_200_02): the BU's default Legal Entity.
        ForeignKeyConstraint(
            ["tenant_id", "default_legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_business_units_default_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
    )

    name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    default_legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class Team(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "business_unit_id"],
            ["business_units.tenant_id", "business_units.id"],
            name="fk_teams_business_unit_id_business_units",
            ondelete="RESTRICT",
        ),
        Index("ix_teams_business_unit_id", "business_unit_id"),
    )

    name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class TeamMember(TenantMixin, AuditedMixin, Base):
    __tablename__ = "team_members"
    __audit_entity_id__ = "team_id"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "team_id"],
            ["teams.tenant_id", "teams.id"],
            name="fk_team_members_team_id_teams",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_team_members_user_id_users",
            ondelete="CASCADE",
        ),
        Index("ix_team_members_user_id", "user_id"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    is_lead: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Role(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """Tenant role; ``permissions`` maps permission key -> scope (``app.core.permissions``).

    ``is_system`` roles are the seeded defaults: never deleted, key fixed; Tenant Admin is immutable.
    """

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("tenant_id", "key"), UniqueConstraint("tenant_id", "id"))

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    permissions: Mapped[dict[str, str]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )


class UserRole(TenantMixin, AuditedMixin, Base):
    __tablename__ = "user_roles"
    __audit_entity_id__ = "user_id"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_user_roles_user_id_users",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["roles.tenant_id", "roles.id"],
            name="fk_user_roles_role_id_roles",
            ondelete="RESTRICT",
        ),
        Index("ix_user_roles_role_id", "role_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
