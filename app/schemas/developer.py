"""Developer Portal DTOs (Epic 74, spec A3 / A4)."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr

OrgRole = Literal["owner", "developer", "viewer"]
SandboxSize = Literal["small", "medium"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ accounts
class SignupIn(_In):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)
    accept_terms_version: str = Field(min_length=1, max_length=20)


class TokenIn(_In):
    token: str = Field(min_length=10, max_length=200)


class LoginIn(_In):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class LoginStepOut(BaseModel):
    step: Literal["mfa_required", "enroll_required"]


class MfaVerifyIn(_In):
    code: str | None = Field(default=None, max_length=10)
    recovery_code: str | None = Field(default=None, max_length=40)


class MfaCodeIn(_In):
    code: str = Field(min_length=6, max_length=10)


class MfaEnrollOut(BaseModel):
    otpauth_uri: str
    secret_display: str


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


class PasswordResetRequestIn(_In):
    email: EmailStr


class PasswordResetConfirmIn(_In):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class CloseAccountIn(_In):
    password: str = Field(min_length=1, max_length=200)


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    verification_status: str
    verified_at: datetime | None = None
    verification_expires_at: datetime | None = None
    role: OrgRole | None = None


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    mfa_enabled: bool
    orgs: list[OrgOut] = Field(default_factory=list)


class SessionOut(BaseModel):
    account: MeOut
    csrf_token: str


# ================================================================ orgs
class OrgCreate(_In):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")


class OrgUpdate(_In):
    name: str = Field(min_length=2, max_length=200)


class MemberOut(BaseModel):
    account_id: uuid.UUID
    name: str
    email: str
    role: OrgRole
    joined_at: datetime


class InviteIn(_In):
    email: EmailStr
    role: OrgRole = "developer"


class InvitationOut(BaseModel):
    id: uuid.UUID
    email: str
    role: OrgRole
    expires_at: datetime


class RoleIn(_In):
    role: OrgRole


class AuditRowOut(BaseModel):
    id: uuid.UUID
    at: datetime
    account_id: uuid.UUID | None
    actor_name: str | None
    action: str
    target_type: str | None
    target_id: uuid.UUID | None
    details: dict[str, Any]


# ================================================================ verification
class SecurityQuestionnaire(_In):
    data_location: str = Field(default="", max_length=300)
    encryption_at_rest: bool = False
    sso_mfa: bool = False
    incident_contact: str = Field(default="", max_length=200)
    pen_test_date: date | None = None
    subprocessors: list[str] = Field(default_factory=list, max_length=50)
    certifications: list[str] = Field(default_factory=list, max_length=20)


class VerificationIn(_In):
    legal_name: str = Field(default="", max_length=200)
    country: str = Field(default="IN", min_length=2, max_length=2)
    tax_id_kind: Literal["gstin", "vat", "ein", "other"] = "gstin"
    tax_id: str = Field(default="", max_length=40)
    website: str = Field(default="", max_length=500)
    registered_address: str = Field(default="", max_length=500)
    support_email: str = Field(default="", max_length=200)
    support_phone: str = Field(default="", max_length=20)
    security_questionnaire: SecurityQuestionnaire = Field(default_factory=SecurityQuestionnaire)
    agreement_version: str = Field(default="", max_length=20)
    evidence_file_ids: list[str] = Field(default_factory=list, max_length=5)


class VerificationOut(BaseModel):
    id: uuid.UUID | None
    partner_org_id: uuid.UUID
    org_name: str | None = None
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None
    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    org_verification_status: str | None = None
    verification_expires_at: datetime | None = None


class DecisionIn(_In):
    decision: Literal["approve", "request_changes", "reject"]
    message: str | None = Field(default=None, max_length=2000)


# ================================================================ sandboxes
class SandboxCreate(_In):
    name: str = Field(min_length=1, max_length=60)
    size: SandboxSize = "small"


class SandboxOut(BaseModel):
    id: uuid.UUID
    name: str
    size: SandboxSize
    status: str
    owner_kind: str
    tenant_id: uuid.UUID | None
    tenant_slug: str | None = None
    failure_code: str | None = None
    created_at: datetime
    ready_at: datetime | None = None
    last_reset_at: datetime | None = None


class SandboxResetIn(_In):
    reinstall_apps: bool = False


class HandoffOut(BaseModel):
    url: str
    expires_in: int


class HandoffIn(_In):
    t: str = Field(min_length=10, max_length=200)


class HandoffResult(BaseModel):
    redirect: str


class SandboxInstallIn(_In):
    app_id: uuid.UUID
    scopes: list[str] = Field(default_factory=list)
    sensitive_purposes: dict[str, str] = Field(default_factory=dict)


class SandboxInstallOut(BaseModel):
    installation_id: uuid.UUID
    status: str
    granted_scopes: list[str]


# ================================================================ playground
class PlaygroundTokenIn(_In):
    app_id: uuid.UUID
    sandbox_id: uuid.UUID
    client_secret: SecretStr
    scope: str | None = Field(default=None, max_length=2000)


class PlaygroundExchangeIn(_In):
    app_id: uuid.UUID
    sandbox_id: uuid.UUID
    code: str = Field(min_length=10, max_length=300)
    code_verifier: str = Field(min_length=43, max_length=128)
    redirect_uri: str = Field(max_length=2000)
    client_secret: SecretStr | None = None


class PlaygroundTokenOut(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str
    refresh_token: str | None = None
    tenant_slug: str


# ================================================================ conformance
class RunStartIn(_In):
    sandbox_id: uuid.UUID


class RunCheckOut(BaseModel):
    key: str
    passed: bool | None
    detail: str | None = None


class RunOut(BaseModel):
    id: uuid.UUID
    app_id: uuid.UUID
    sandbox_id: uuid.UUID
    installation_id: uuid.UUID
    status: str
    manifest_sha256: str
    window_ends_at: datetime
    created_at: datetime
    finished_at: datetime | None
    checks: list[RunCheckOut] = Field(default_factory=list)
    report_available: bool = False


# ================================================================ submissions
class SubmitIn(_In):
    conformance_run_id: uuid.UUID


# ================================================================ usage
class UsageDay(BaseModel):
    day: date
    requests: int = 0
    errors: int = 0
    webhook_attempts: int = 0
    webhook_failures: int = 0
    deprecated_calls: int = 0


class UsageTenantRow(BaseModel):
    label: str
    requests: int


class AppUsageOut(BaseModel):
    app_id: uuid.UUID
    date_from: date
    date_to: date
    installs_total: int
    installs_active: int
    requests: int
    error_rate: float
    webhook_attempts: int
    webhook_success_rate: float | None
    deprecated_calls: int
    days: list[UsageDay]
    tenants: list[UsageTenantRow] = Field(default_factory=list)


# ================================================================ platform directory
class AccountDirectoryRow(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    status: str
    email_verified: bool
    mfa_enabled: bool
    created_at: datetime
    last_login_at: datetime | None


__all__ = [
    "AccountDirectoryRow",
    "AppUsageOut",
    "AuditRowOut",
    "CloseAccountIn",
    "DecisionIn",
    "HandoffIn",
    "HandoffOut",
    "HandoffResult",
    "InvitationOut",
    "InviteIn",
    "LoginIn",
    "LoginStepOut",
    "MeOut",
    "MemberOut",
    "MfaCodeIn",
    "MfaEnrollOut",
    "MfaVerifyIn",
    "OrgCreate",
    "OrgOut",
    "OrgRole",
    "OrgUpdate",
    "PasswordResetConfirmIn",
    "PasswordResetRequestIn",
    "PlaygroundExchangeIn",
    "PlaygroundTokenIn",
    "PlaygroundTokenOut",
    "RecoveryCodesOut",
    "RoleIn",
    "RunCheckOut",
    "RunOut",
    "RunStartIn",
    "SandboxCreate",
    "SandboxInstallIn",
    "SandboxInstallOut",
    "SandboxOut",
    "SandboxResetIn",
    "SecurityQuestionnaire",
    "SessionOut",
    "SignupIn",
    "SubmitIn",
    "TokenIn",
    "UsageDay",
    "UsageTenantRow",
    "VerificationIn",
    "VerificationOut",
]
