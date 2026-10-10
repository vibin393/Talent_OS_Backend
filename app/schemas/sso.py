"""Single sign-on (Epic 14, FR-77, AD-30). The IdP client secret is write-only: never returned."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.identity import Email, TenantSlug

IdpKind = Literal["oidc", "saml"]
IdpStatus = Literal["draft", "active", "disabled"]
EnforceMode = Literal["none", "all", "roles"]
Domain = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=3,
        max_length=253,
        pattern=r"^[a-z0-9.-]+\.[a-z]{2,}$",
    ),
]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OidcConfig(_In):
    issuer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=500)]
    client_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"])
    # Optional overrides of the discovery document.
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    jwks_uri: str | None = None


class SamlConfig(_In):
    idp_entity_id: ShortText
    sso_url: ShortText
    x509_certs: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=20, max_length=10000)]
    ] = Field(min_length=1)
    email_attribute: str | None = None  # default: the NameID
    name_attribute: str | None = None


class IdentityProviderIn(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    kind: IdpKind
    email_domains: list[Domain] = Field(default_factory=list)
    oidc: OidcConfig | None = None
    saml: SamlConfig | None = None
    client_secret: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    enforce_for: EnforceMode = "none"
    enforced_role_ids: list[uuid.UUID] = Field(default_factory=list)
    jit_enabled: bool = False
    jit_default_role_id: uuid.UUID | None = None
    group_claim: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    mfa_trusted: bool = False


class IdentityProviderUpdate(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    status: IdpStatus | None = None
    email_domains: list[Domain] | None = None
    oidc: OidcConfig | None = None
    saml: SamlConfig | None = None
    client_secret: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    enforce_for: EnforceMode | None = None
    enforced_role_ids: list[uuid.UUID] | None = None
    jit_enabled: bool | None = None
    jit_default_role_id: uuid.UUID | None = None
    group_claim: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    mfa_trusted: bool | None = None


class GroupRoleIn(_In):
    group_value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    role_id: uuid.UUID


class GroupRoleOut(BaseModel):
    group_value: str
    role_id: uuid.UUID


class IdentityProviderOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: IdpKind
    status: IdpStatus
    email_domains: list[str]
    config: dict[str, Any]
    has_secret: bool
    enforce_for: EnforceMode
    enforced_role_ids: list[uuid.UUID]
    jit_enabled: bool
    jit_default_role_id: uuid.UUID | None
    group_claim: str | None
    mfa_trusted: bool
    linked_users: int
    group_roles: list[GroupRoleOut]
    # Values the admin copies into the IdP.
    redirect_uri: str | None = None
    sp_entity_id: str | None = None
    acs_url: str | None = None
    metadata_url: str | None = None
    created_at: datetime
    updated_at: datetime


class TestLoginOut(BaseModel):
    url: str


class BreakGlassAdminOut(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str
    is_active: bool
    mfa_enabled: bool
    break_glass: bool


class BreakGlassIn(_In):
    break_glass: bool


# ---------------------------------------------------------------- public login
class SsoDiscoverIn(_In):
    tenant_slug: TenantSlug
    email: Email


class SsoDiscoverOut(BaseModel):
    """Identical shape for unknown emails (domain match only)."""

    sso: bool
    idp_id: uuid.UUID | None = None
    required: bool = False


class SsoTestResultOut(BaseModel):
    test: Literal[True] = True
    subject: str
    email: str | None
    email_verified: bool
    name: str | None
    groups: list[str]
    resolution: Literal["linked", "link_by_email", "jit", "refused"]
