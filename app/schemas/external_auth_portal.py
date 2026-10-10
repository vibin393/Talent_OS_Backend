"""Client Portal and Vendor Portal sign-in / self-administration schemas (Epic 14, FR-78, FR-79, FR-80).

Every response model here is registered with the FR-80 framework (``register_portal_schema``) for the
principal that receives it; ``tests/portal/test_field_visibility.py`` walks them.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.field_visibility import register_portal_schema
from app.schemas.identity import Email, Password, RecoveryCode, TenantSlug, TotpCode

SignInCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9]{6}$")]
Token = Annotated[str, StringConstraints(min_length=10, max_length=512)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- requests (shared)
class PortalLinkRequestIn(_In):
    tenant_slug: TenantSlug
    email: Email


class PortalLinkVerifyIn(_In):
    token: Token


class PortalCodeVerifyIn(_In):
    tenant_slug: TenantSlug
    email: Email
    code: SignInCode


class PortalPasswordLoginIn(_In):
    tenant_slug: TenantSlug
    email: Email
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class PortalInviteAcceptIn(_In):
    token: Token
    display_name: DisplayName | None = None
    password: Password


class PortalPasswordResetRequestIn(_In):
    tenant_slug: TenantSlug
    email: Email


class PortalPasswordResetConfirmIn(_In):
    token: Token
    password: Password


class PortalMfaIn(_In):
    """Exactly one of ``code`` (TOTP) or ``recovery_code``."""

    code: TotpCode | None = None
    recovery_code: RecoveryCode | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.code is None) == (self.recovery_code is None):
            msg = "Provide exactly one of code or recovery_code"
            raise ValueError(msg)
        return self


class PortalMfaCodeIn(_In):
    code: TotpCode


# ---------------------------------------------------------------- responses
@register_portal_schema(("client_user", "vendor_user"))
class PortalStatusOut(BaseModel):
    status: Literal["check_email", "ok"]


@register_portal_schema(("client_user", "vendor_user"))
class PortalMfaEnrollmentOut(BaseModel):
    secret: str
    otpauth_uri: str


@register_portal_schema(("client_user", "vendor_user"))
class PortalIdentityOut(BaseModel):
    principal_type: Literal["client_user", "vendor_user"]
    id: uuid.UUID
    display_name: str
    email: str
    organization_name: str
    portal_permissions: list[str]
    is_admin: bool
    mfa_enabled: bool


@register_portal_schema(("client_user", "vendor_user"))
class PortalAuthOut(BaseModel):
    """``signed_in`` (cookies set), ``mfa_required`` (send a code) or ``mfa_enrollment_required`` (scan the
    secret, then confirm a code). Until signed in no session exists."""

    status: Literal["signed_in", "mfa_required", "mfa_enrollment_required"]
    me: PortalIdentityOut | None = None
    enrollment: PortalMfaEnrollmentOut | None = None
    recovery_codes: list[str] = Field(default_factory=list)


@register_portal_schema("vendor_user")
class PortalInviteOut(BaseModel):
    email: str
    display_name: str
    organization_name: str
    tenant_name: str


@register_portal_schema(("client_user", "vendor_user"))
class PortalRecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


# ---------------------------------------------------------------- vendor self-administration
class VendorPortalUserInviteIn(_In):
    email: Email
    display_name: DisplayName
    is_vendor_admin: bool = False
    portal_permissions: list[
        Annotated[str, StringConstraints(pattern=r"^[a-z_]+:[a-z_]+$", max_length=60)]
    ] = Field(default_factory=list)


class VendorPortalUserUpdate(_In):
    display_name: DisplayName | None = None
    is_vendor_admin: bool | None = None
    portal_permissions: (
        list[Annotated[str, StringConstraints(pattern=r"^[a-z_]+:[a-z_]+$", max_length=60)]] | None
    ) = None


@register_portal_schema("vendor_user")
class VendorPortalUserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    status: Literal["invited", "active", "suspended", "deactivated"]
    is_vendor_admin: bool
    portal_permissions: list[str]
    mfa_enabled: bool
    last_sign_in_at: datetime | None
