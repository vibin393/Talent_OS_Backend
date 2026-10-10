"""Client Portal SSO (Epic 50, FR-250..FR-252, AD-48): per-Client identity providers (admin shapes) and the
Client Portal discovery shape. The IdP client secret is write-only: never returned."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.identity import Email, TenantSlug
from app.schemas.sso import Domain, IdpKind, IdpStatus, OidcConfig, SamlConfig

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Secret = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
PortalPermission = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- identity providers (internal admin)
class ClientIdentityProviderIn(_In):
    name: Name
    kind: IdpKind
    # AD-48 ``allowed_domains`` == ``identity_providers.email_domains``; required non-empty for a client IdP.
    email_domains: list[Domain] = Field(default_factory=list)
    oidc: OidcConfig | None = None
    saml: SamlConfig | None = None
    client_secret: Secret | None = None
    jit_enabled: bool = False
    jit_default_permissions: list[PortalPermission] = Field(default_factory=list)
    mfa_trusted: bool = False


class ClientIdentityProviderUpdate(_In):
    name: Name | None = None
    status: IdpStatus | None = None
    email_domains: list[Domain] | None = None
    oidc: OidcConfig | None = None
    saml: SamlConfig | None = None
    client_secret: Secret | None = None
    jit_enabled: bool | None = None
    jit_default_permissions: list[PortalPermission] | None = None
    mfa_trusted: bool | None = None


class ClientIdentityProviderOut(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    name: str
    kind: IdpKind
    status: IdpStatus
    email_domains: list[str]
    config: dict[str, Any]
    has_secret: bool
    jit_enabled: bool
    jit_default_permissions: list[str]
    mfa_trusted: bool
    linked_users: int
    last_login_at: datetime | None = None
    # Values the admin copies into the IdP (same callback / ACS as internal IdPs).
    redirect_uri: str | None = None
    sp_entity_id: str | None = None
    acs_url: str | None = None
    # Non-blocking validation findings, e.g. ``certificate_expires_soon``.
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class MetadataImportIn(_In):
    """SAML IdP metadata: exactly one of ``metadata_url`` (https) or ``metadata_xml``."""

    metadata_url: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=12, max_length=1000)] | None
    ) = None
    metadata_xml: Annotated[str, StringConstraints(min_length=20, max_length=500_000)] | None = None

    @model_validator(mode="after")
    def _one(self) -> "MetadataImportIn":
        if (self.metadata_url is None) == (self.metadata_xml is None):
            msg = "Provide either metadata_url or metadata_xml"
            raise ValueError(msg)
        return self


class ClientTestLoginOut(BaseModel):
    url: str


# ---------------------------------------------------------------- enforcement and break-glass
class ClientSsoSettingsIn(_In):
    sso_required: bool | None = None
    approval_links_allowed: bool | None = None


class ClientSsoSettingsOut(BaseModel):
    client_id: uuid.UUID
    sso_required: bool
    approval_links_allowed: bool
    has_active_idp: bool
    exemption_count: int
    # ``no_break_glass`` when SSO is required and no Client User is exempt.
    warnings: list[str] = Field(default_factory=list)
    sign_in_url: str
    updated_at: datetime | None = None


class ClientSsoExemptionIn(_In):
    client_user_id: uuid.UUID
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=1000)]


class ClientSsoExemptionOut(BaseModel):
    client_user_id: uuid.UUID
    display_name: str
    email: str
    status: str
    reason: str
    mfa_enforced: bool
    mfa_enabled: bool
    created_at: datetime


# ---------------------------------------------------------------- Client Portal (public)
class ClientSsoDiscoverIn(_In):
    tenant_slug: TenantSlug
    email: Email


class ClientSsoDiscoverOut(BaseModel):
    """Identical shape for unknown tenants, unknown domains and domains without client SSO."""

    sso: bool
    idp_id: uuid.UUID | None = None
    required: bool = False


ClientResolution = Literal["linked", "email_match", "jit", "refused"]


class ClientSsoTestResultOut(BaseModel):
    """Test sign-in of a client IdP: the mapped claims and how the login would resolve (creates nothing)."""

    test: Literal[True] = True
    audience: Literal["client"] = "client"
    subject: str
    email: str | None
    email_verified: bool
    name: str | None
    groups: list[str]
    resolution: ClientResolution
    # ``refused`` only: ``email_unverified`` | ``domain_not_allowed`` | ``other_client`` |
    # ``other_principal`` |
    # ``inactive`` | ``no_client_user``.
    reason: str | None = None
    client_user_id: uuid.UUID | None = None
