import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Query
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

RESERVED_SLUGS = frozenset({"www", "api", "admin", "app"})
# 3..63 chars, lowercase letters/digits/hyphens, no leading/trailing hyphen (DNS-label safe).
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
LOCALE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")

DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_CURRENCY = "INR"
DEFAULT_LOCALE = "en-IN"

Email = Annotated[EmailStr, StringConstraints(max_length=254)]


# ---------------------------------------------------------------- auth
class OperatorLogin(BaseModel):
    email: Email
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class OperatorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    mfa_enabled: bool
    last_login_at: datetime | None


# ---------------------------------------------------------------- tenants
class TenantCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    slug: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=63)]
    timezone: str = DEFAULT_TIMEZONE
    currency: Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True)] = DEFAULT_CURRENCY
    locale: str = DEFAULT_LOCALE
    seat_limit: Annotated[int, Field(ge=1, le=1_000_000)]
    admin_email: Email

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.fullmatch(v):
            msg = "slug must be 3-63 characters: lowercase letters, digits and hyphens, not at the ends"
            raise ValueError(msg)
        if v in RESERVED_SLUGS:
            msg = f"slug '{v}' is reserved"
            raise ValueError(msg)
        return v

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:  # "America" -> PermissionError
            msg = "unknown IANA timezone"
            raise ValueError(msg) from exc
        return v

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Z]{3}", v):
            msg = "currency must be a 3-letter ISO 4217 code"
            raise ValueError(msg)
        return v

    @field_validator("locale")
    @classmethod
    def _locale(cls, v: str) -> str:
        if not LOCALE_RE.fullmatch(v):
            msg = "locale must look like 'en' or 'en-IN'"
            raise ValueError(msg)
        return v

    @field_validator("admin_email")
    @classmethod
    def _lower_email(cls, v: str) -> str:
        return v.lower()


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: str
    timezone: str
    currency: str
    locale: str
    seat_limit: int
    mfa_required: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------- tenant users (story 1.3)
# Password strength (length, common-password list) is checked by the service so every violation
# returns 422 with ``details.fields.password``; the schema only bounds the size.
Password = Annotated[str, StringConstraints(max_length=1024)]
TenantSlug = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=63)
]
PersonName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
OpaqueToken = Annotated[str, StringConstraints(min_length=1, max_length=512)]


class TenantSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    timezone: str
    currency: str
    locale: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    name: str
    # Derived from ``user_roles`` (kept for frontend compatibility since story 1.5).
    role_keys: list[str]
    business_unit_id: uuid.UUID | None = None
    mfa_enabled: bool
    last_login_at: datetime | None


class SupportSessionInfo(BaseModel):
    """Epic 12 support-access session details shown in the tenant app's read-only support banner."""

    operator_name: str
    expires_at: datetime


class AuthOut(BaseModel):
    """Body of a successful sign-in / invitation acceptance / ``GET /auth/me``.

    ``GET /auth/me`` also answers support-access sessions (``tos_support``): ``principal_type="support"``,
    ``support`` set, ``user`` describing the platform operator (no roles) and read-only ``permissions``.
    """

    principal_type: Literal["user", "support"] = "user"
    support: SupportSessionInfo | None = None
    user: UserOut
    tenant: TenantSummary
    # permission -> widest granted scope (``own|team|bu|tenant``)
    permissions: dict[str, str] = Field(default_factory=dict)
    # The tenant requires MFA and the user has not enrolled: only ``auth/me``, ``auth/logout`` and
    # ``auth/mfa/*`` are usable (every other endpoint returns 403 ``mfa_enrollment_required``).
    mfa_enrollment_required: bool = False


# ---------------------------------------------------------------- MFA (story 1.4)
TotpCode = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]
RecoveryCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class MfaRequiredOut(BaseModel):
    """Password accepted for an MFA principal; finish with ``POST .../auth/mfa/verify`` (``tos_mfa``)."""

    mfa_required: Literal[True] = True


class MfaEnrollOut(BaseModel):
    secret: str  # base32, for manual entry
    otpauth_uri: str  # render as a QR code


class MfaCodeIn(BaseModel):
    code: TotpCode


class MfaVerifyIn(BaseModel):
    """Exactly one of ``code`` (TOTP) or ``recovery_code``."""

    code: TotpCode | None = None
    recovery_code: RecoveryCode | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.code is None) == (self.recovery_code is None):
            msg = "Provide exactly one of code or recovery_code"
            raise ValueError(msg)
        return self


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]  # shown once; only hashes are stored


class TenantSecurityOut(BaseModel):
    mfa_required: bool


class TenantSecurityUpdate(BaseModel):
    mfa_required: bool


class UserLogin(BaseModel):
    # Optional: when omitted, the tenant is inferred from the request subdomain (Epic 6.2).
    tenant_slug: TenantSlug | None = None
    email: Email
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class InvitationOut(BaseModel):
    email: str
    name: str | None = None
    tenant: TenantSummary
    expires_at: datetime


class InvitationAccept(BaseModel):
    name: PersonName
    password: Password


class InvitationTokenIn(BaseModel):
    """Epic 12 (A10): invitation tokens travel in request bodies, never in URLs."""

    token: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class InvitationAcceptWithToken(InvitationAccept):
    token: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class PasswordResetRequest(BaseModel):
    tenant_slug: TenantSlug
    email: Email


class PasswordResetConfirm(BaseModel):
    token: OpaqueToken
    password: Password


# ---------------------------------------------------------------- administration (story 1.5)
ROLE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
ROLE_KEY_MESSAGE = (
    "key must be 2-64 characters: lowercase letters, digits and underscores, starting with a letter"
)
OrgName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]
IdList = Annotated[list[uuid.UUID], Field(max_length=100)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class UserFilters:
    """Query filters of ``GET /admin/users``; use as ``Annotated[UserFilters, Depends()]``."""

    q: Annotated[str | None, Query(max_length=200, description="Name or email contains")] = None
    is_active: bool | None = None
    role_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None
    business_unit_id: uuid.UUID | None = None


class RoleRef(BaseModel):
    id: uuid.UUID
    key: str
    name: str


class TeamRef(BaseModel):
    id: uuid.UUID
    name: str
    is_lead: bool


class BusinessUnitRef(BaseModel):
    id: uuid.UUID
    name: str


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    is_active: bool
    role_keys: list[str]
    roles: list[RoleRef]
    business_unit: BusinessUnitRef | None
    teams: list[TeamRef]
    mfa_enabled: bool
    last_login_at: datetime | None
    created_at: datetime


class UserInvite(_Strict):
    email: Email
    name: PersonName | None = None
    role_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=100)]
    business_unit_id: uuid.UUID | None = None
    team_ids: IdList = Field(default_factory=list)

    @field_validator("email")
    @classmethod
    def _lower_email(cls, v: str) -> str:
        return v.lower()


class UserUpdate(_Strict):
    """Partial update; only the fields sent are changed (``business_unit_id: null`` clears it)."""

    name: PersonName | None = None
    role_ids: IdList | None = None
    business_unit_id: uuid.UUID | None = None
    team_ids: IdList | None = None


class InvitationAdminOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None = None
    role_ids: list[uuid.UUID]
    business_unit_id: uuid.UUID | None
    team_ids: list[uuid.UUID]
    invited_by_type: str
    expires_at: datetime
    expired: bool
    created_at: datetime


class PermissionOut(BaseModel):
    key: str
    group: str
    description: str
    scopes: list[str]
    # Epic 14 (FR-81): never added to custom Roles automatically; badged "Sensitive" in the Roles UI.
    sensitive: bool = False


class RoleOut(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    description: str | None
    is_system: bool
    is_immutable: bool
    permissions: dict[str, str]
    user_count: int
    created_at: datetime
    updated_at: datetime


class RoleCreate(_Strict):
    key: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=64)]
    name: OrgName
    description: Description | None = None
    permissions: dict[str, str] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not ROLE_KEY_RE.fullmatch(v):
            msg = ROLE_KEY_MESSAGE
            raise ValueError(msg)
        return v


class RoleUpdate(_Strict):
    key: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=64)] | None = None
    name: OrgName | None = None
    description: Description | None = None
    permissions: dict[str, str] | None = None

    @field_validator("key")
    @classmethod
    def _key(cls, v: str | None) -> str | None:
        if v is not None and not ROLE_KEY_RE.fullmatch(v):
            msg = ROLE_KEY_MESSAGE
            raise ValueError(msg)
        return v


class BusinessUnitCreate(_Strict):
    name: OrgName
    description: Description | None = None
    # Epic 14 (FR-76): must be an active Legal Entity.
    default_legal_entity_id: uuid.UUID | None = None


class BusinessUnitUpdate(_Strict):
    name: OrgName | None = None
    description: Description | None = None
    default_legal_entity_id: uuid.UUID | None = None


class BusinessUnitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    default_legal_entity_id: uuid.UUID | None = None


class TeamCreate(_Strict):
    name: OrgName
    description: Description | None = None
    business_unit_id: uuid.UUID | None = None


class TeamUpdate(_Strict):
    name: OrgName | None = None
    description: Description | None = None
    business_unit_id: uuid.UUID | None = None


class TeamMemberOut(BaseModel):
    user_id: uuid.UUID
    name: str
    # Only for principals holding ``user:read`` (else null).
    email: str | None
    is_active: bool | None
    is_lead: bool


class TeamOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    business_unit_id: uuid.UUID | None
    member_count: int
    created_at: datetime
    updated_at: datetime


class TeamDetailOut(TeamOut):
    members: list[TeamMemberOut]


class TeamMemberSet(_Strict):
    is_lead: bool = False
