# ruff: file-ignore[line-too-long]
"""Developer Portal platform tables (Epic 74, spec A2; migrations ``20261010_508_01..03``).

All are **platform** tables (``core.tenancy.PLATFORM_TABLES``; no RLS; explicit ``talent_app`` grants). They hold
developer contact data under the platform privacy notice only, never Tenant data. ``partner_orgs`` (the 66 hub) is
ALTERed by 508_01; its new columns live on ``app.models.oauth.PartnerOrg``.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, Timestamps, UUIDPk
from app.models.identity import LoginThrottle

ACCOUNT_STATUSES = ("active", "locked", "closed")
ORG_ROLES = ("owner", "developer", "viewer")
EMAIL_TOKEN_PURPOSES = ("verify_email", "reset_password")
VERIFICATION_STATUSES = ("draft", "pending", "changes_requested", "approved", "rejected")
TAX_ID_KINDS = ("gstin", "vat", "ein", "other")
SANDBOX_OWNER_KINDS = ("partner_org", "tenant")
SANDBOX_SIZES = ("small", "medium")
SANDBOX_STATUSES = ("creating", "ready", "resetting", "deleting", "deleted", "failed")
SANDBOX_MEMBER_ROLES = ("admin", "viewer")
RUN_STATUSES = ("waiting", "running", "finishing", "passed", "failed", "expired", "cancelled")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class DeveloperAccount(UUIDPk, LoginThrottle, Timestamps, Base):
    __tablename__ = "developer_accounts"
    __table_args__ = (UniqueConstraint("email"), CheckConstraint(_in("status", ACCOUNT_STATUSES), name="status"))

    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text(), nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mfa_secret_enc: Mapped[str | None] = mapped_column(Text(), nullable=True)
    mfa_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    terms_version: Mapped[str] = mapped_column(String(20), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeveloperRecoveryCode(UUIDPk, Timestamps, Base):
    __tablename__ = "developer_recovery_codes"
    __table_args__ = (UniqueConstraint("account_id", "code_hash"),)

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("developer_accounts.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeveloperSession(UUIDPk, Base):
    __tablename__ = "developer_sessions"
    __table_args__ = (UniqueConstraint("token_hash"), Index("ix_developer_sessions_account_id_revoked_at", "account_id", "revoked_at"))

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("developer_accounts.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mfa_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)


class DeveloperEmailToken(UUIDPk, Timestamps, Base):
    __tablename__ = "developer_email_tokens"
    __table_args__ = (UniqueConstraint("token_hash"), CheckConstraint(_in("purpose", EMAIL_TOKEN_PURPOSES), name="purpose"))

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("developer_accounts.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PartnerOrgMember(UUIDPk, Timestamps, Base):
    __tablename__ = "partner_org_members"
    __table_args__ = (
        CheckConstraint(_in("role", ORG_ROLES), name="role"),
        Index(
            "uq_partner_org_members_org_account_live", "partner_org_id", "account_id", unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
        Index("ix_partner_org_members_account_id", "account_id"),
    )

    partner_org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("partner_orgs.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("developer_accounts.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PartnerOrgInvitation(UUIDPk, Timestamps, Base):
    __tablename__ = "partner_org_invitations"
    __table_args__ = (
        UniqueConstraint("token_hash"),
        CheckConstraint(_in("role", ORG_ROLES), name="role"),
        Index("ix_partner_org_invitations_org_accepted", "partner_org_id", "accepted_at"),
    )

    partner_org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("partner_orgs.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    invited_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PartnerVerificationRequest(UUIDPk, Timestamps, Base):
    __tablename__ = "partner_verification_requests"
    __table_args__ = (
        CheckConstraint(_in("status", VERIFICATION_STATUSES), name="status"),
        Index(
            "uq_partner_verification_requests_open", "partner_org_id", unique=True,
            postgresql_where=text("status IN ('draft', 'pending', 'changes_requested')"),
        ),
    )

    partner_org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("partner_orgs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    evidence_file_keys: Mapped[list[str]] = mapped_column(ARRAY(Text()), nullable=False, server_default=text("'{}'"))
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str | None] = mapped_column(String(2000), nullable=True)


class PartnerOrgAuditLog(UUIDPk, Base):
    """Append-only (retention 3 years). Never stores secrets, codes or tokens."""

    __tablename__ = "partner_org_audit_log"

    partner_org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    operator_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DeveloperSandbox(UUIDPk, Timestamps, Base):
    __tablename__ = "developer_sandboxes"
    __table_args__ = (
        CheckConstraint(_in("owner_kind", SANDBOX_OWNER_KINDS), name="owner_kind"),
        CheckConstraint(_in("size", SANDBOX_SIZES), name="size"),
        CheckConstraint(_in("status", SANDBOX_STATUSES), name="status"),
        CheckConstraint(
            "((owner_kind = 'partner_org') = (partner_org_id IS NOT NULL)) AND "
            "((owner_kind = 'tenant') = (owner_tenant_id IS NOT NULL))",
            name="owner",
        ),
        Index("ix_developer_sandboxes_partner_org_id_status", "partner_org_id", "status"),
        Index("ix_developer_sandboxes_owner_tenant_id_status", "owner_tenant_id", "status"),
        Index("ix_developer_sandboxes_tenant_id", "tenant_id"),
    )

    owner_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    partner_org_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("partner_orgs.id", ondelete="CASCADE"), nullable=True
    )
    owner_tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    size: Mapped[str] = mapped_column(String(6), nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'creating'"))
    failure_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeveloperSandboxMember(UUIDPk, Timestamps, Base):
    __tablename__ = "developer_sandbox_members"
    __table_args__ = (
        UniqueConstraint("sandbox_id", "account_id", name="uq_developer_sandbox_members_sandbox_account"),
        UniqueConstraint("sandbox_id", "owner_user_id", name="uq_developer_sandbox_members_sandbox_owner_user"),
        CheckConstraint(_in("role", SANDBOX_MEMBER_ROLES), name="role"),
    )

    sandbox_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("developer_sandboxes.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sandbox_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)


class ConformanceRun(UUIDPk, Timestamps, Base):
    __tablename__ = "conformance_runs"
    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        Index("ix_conformance_runs_app_id_created_at", "app_id", "created_at"),
    )

    app_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), nullable=False)
    sandbox_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("developer_sandboxes.id", ondelete="CASCADE"), nullable=False
    )
    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    started_by_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    window_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observations: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "ACCOUNT_STATUSES",
    "ORG_ROLES",
    "RUN_STATUSES",
    "SANDBOX_STATUSES",
    "TAX_ID_KINDS",
    "ConformanceRun",
    "DeveloperAccount",
    "DeveloperEmailToken",
    "DeveloperRecoveryCode",
    "DeveloperSandbox",
    "DeveloperSandboxMember",
    "DeveloperSession",
    "PartnerOrgAuditLog",
    "PartnerOrgInvitation",
    "PartnerOrgMember",
    "PartnerVerificationRequest",
]
