"""External principals (Epic 13, AD-10, AD-26): Client Users, their visibility scopes, Vendor Users, and
explicit organization grants (``org_grants``, read by ``core.org_grants`` and the org RLS policies).

Tenant-owned with forced tenant RLS and the restrictive ``org_isolation`` policy (migration
20261002_200_03). Sign-in, invitations and MFA enrolment are Epic 14.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CLIENT_USER_STATUSES = ("invited", "active", "suspended", "revoked")
VENDOR_USER_STATUSES = ("invited", "active", "suspended", "deactivated")
CLIENT_PORTAL_PERMISSIONS = (
    "submissions:review",
    "demands:approve",
    "time:approve",
    "placements:view",
    "invoices:view",
    # Epic 45 (FR-231): Client Portal projects and deliverable acceptance.
    "projects:view",
    "deliverables:accept",
    # Epic 44 (FR-226): Client Portal hiring forecasts.
    "forecasts:submit",
    # Epic 77 (FR-396): the portal Copilot (default not granted).
    "portal_copilot:use",
)
SCOPE_SUBJECT_KINDS = ("demand", "placement")
GRANT_ORG_KINDS = ("client", "vendor", "worker")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class ClientUser(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "client_users"
    __pii_fields__ = ("email", "display_name")
    __audit_exclude__ = ("mfa_secret", "mfa_last_step", "last_sign_in_at")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "contact_id"),
        CheckConstraint(_in("status", CLIENT_USER_STATUSES), name="status"),
        _tenant_fk("client_id", "clients", "fk_client_users_client_id_clients"),
        _tenant_fk("contact_id", "contacts", "fk_client_users_contact_id_contacts"),
        _tenant_fk("invited_by", "users", "fk_client_users_invited_by_users"),
        Index("ix_client_users_client_id", "client_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'invited'"))
    portal_permissions: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    mfa_enforced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # Fernet purpose ``mfa`` (same as users).
    mfa_secret: Mapped[str | None] = mapped_column(Text(), nullable=True)
    mfa_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sign_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 14 (migration 20261003_201_03): last accepted TOTP step (replay guard).
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)


class ClientUserScope(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """``client_id`` is copied from the Client User by the ``tos_inherit_org`` trigger."""

    __tablename__ = "client_user_scopes"
    __table_args__ = (
        UniqueConstraint("client_user_id", "subject_kind", "subject_id"),
        CheckConstraint(_in("subject_kind", SCOPE_SUBJECT_KINDS), name="subject_kind"),
        _tenant_fk(
            "client_user_id", "client_users", "fk_client_user_scopes_client_user_id_client_users", "CASCADE"
        ),
    )

    client_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class VendorUser(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "vendor_users"
    __pii_fields__ = ("email", "display_name")
    __audit_exclude__ = (
        "mfa_secret",
        "password_hash",
        "mfa_last_step",
        "failed_login_count",
        "first_failed_at",
        "locked_until",
        "last_sign_in_at",
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "email"),
        CheckConstraint(_in("status", VENDOR_USER_STATUSES), name="status"),
        _tenant_fk("vendor_id", "vendors", "fk_vendor_users_vendor_id_vendors"),
        Index("ix_vendor_users_vendor_id", "vendor_id"),
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'invited'"))
    is_vendor_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    portal_permissions: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    mfa_secret: Mapped[str | None] = mapped_column(Text(), nullable=True)
    mfa_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sign_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 14 (migration 20261003_201_03): TOTP replay guard, password lockout, inviter, password age.
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    first_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_by_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrgGrant(UUIDPk, TenantMixin, Timestamps, Base):
    """One external organization may see one internal subject (``core.org_grants``)."""

    __tablename__ = "org_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "org_kind", "org_id", "subject_kind", "subject_id"),
        CheckConstraint(_in("org_kind", GRANT_ORG_KINDS), name="org_kind"),
        Index("ix_org_grants_subject", "subject_kind", "subject_id"),
    )

    org_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    granted_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
