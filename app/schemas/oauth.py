# ruff: file-ignore[line-too-long]
"""OAuth / App DTOs (Epic 66 foundation; Epic 67 extends the protocol and App-management models additively)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GrantType = Literal["authorization_code", "client_credentials", "refresh_token"]
Resource = Literal["api", "mcp"]
AppStatus = Literal["development", "published", "suspended", "retired"]


class IssuedTokens(BaseModel):
    """Plaintext tokens, returned exactly once by ``issue_token`` (never logged, never stored)."""

    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str | None = None
    expires_in: int
    scopes: list[str]
    token_id: uuid.UUID
    family_id: uuid.UUID

    def __repr__(self) -> str:  # never print tokens
        return f"IssuedTokens(token_id={self.token_id}, scopes={self.scopes})"

    __str__ = __repr__


class UserAuthorizationOut(BaseModel):
    id: uuid.UUID
    installation_id: uuid.UUID
    app_id: uuid.UUID | None
    app_name: str
    resource: Resource
    scopes: list[str]
    granted_at: datetime
    revoked_at: datetime | None = None


class Publisher(BaseModel):
    """Who registers an App: a tenant (private App), a partner organisation (74) or the platform."""

    kind: Literal["tenant", "partner", "platform"]
    tenant_id: uuid.UUID | None = None
    partner_org_id: uuid.UUID | None = None
    created_by_kind: Literal["tenant_user", "developer", "platform"] = "tenant_user"
    created_by_id: uuid.UUID | None = None


class AppCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    client_type: Literal["confidential", "public"] = "confidential"
    grant_types: list[GrantType] = Field(default_factory=lambda: ["client_credentials"])  # type: ignore[arg-type]
    redirect_uris: list[str] = Field(default_factory=list)
    resources: list[Resource] = Field(default_factory=lambda: ["api"])  # type: ignore[arg-type]
    requested_scopes: list[str] = Field(min_length=1)
    scope_reasons: dict[str, str] = Field(default_factory=dict)
    homepage_url: str | None = None
    privacy_url: str | None = None
    support_email: str | None = None
    # Epic 67 (additive): description (stored in the manifest), the AppManifest data flows / events consumed.
    description: str | None = Field(default=None, max_length=1000)
    manifest: dict[str, Any] | None = None


class AppUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    redirect_uris: list[str] | None = None
    requested_scopes: list[str] | None = None
    scope_reasons: dict[str, str] | None = None
    # Epic 67 (additive)
    description: str | None = Field(default=None, max_length=1000)
    resources: list[Resource] | None = None
    homepage_url: str | None = None
    privacy_url: str | None = None
    support_email: str | None = None
    manifest: dict[str, Any] | None = None


class AppOut(BaseModel):
    id: uuid.UUID
    client_id: str
    name: str
    slug: str
    publisher_kind: str
    client_type: str
    grant_types: list[str]
    resources: list[str]
    requested_scopes: list[str]
    status: AppStatus
    manifest_version: int
    created_at: datetime
    # Epic 67 (additive)
    description: str | None = None
    redirect_uris: list[str] = Field(default_factory=list)
    scope_reasons: dict[str, str] = Field(default_factory=dict)
    homepage_url: str | None = None
    privacy_url: str | None = None
    support_email: str | None = None
    owner_tenant_id: uuid.UUID | None = None
    reconsent_installations: int = 0


class SecretCreated(BaseModel):
    secret_id: uuid.UUID
    client_secret: str  # shown once
    fingerprint: str
    expires_at: datetime | None = None
    prefix: str = ""  # Epic 67 (additive)
    previous_expires_at: datetime | None = None


class AppCreated(AppOut):
    client_secret: str | None = None  # shown once (confidential clients)


# ---------------------------------------------------------------- protocol (Epic 67 fills the shapes)
class ClientAuth(BaseModel):
    client_id: str
    client_secret: str | None = None


class TokenRequest(BaseModel):
    grant_type: GrantType
    code: str | None = None
    redirect_uri: str | None = None
    code_verifier: str | None = None
    refresh_token: str | None = None
    scope: str | None = None
    resource: str | None = None
    tenant: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    refresh_token: str | None = None
    scope: str


class AuthorizeRequest(BaseModel):
    client_id: str
    redirect_uri: str
    response_type: Literal["code"] = "code"
    scope: str
    state: str
    code_challenge: str
    code_challenge_method: Literal["S256"] = "S256"
    resource: str | None = None


class ConsentScope(BaseModel):
    key: str
    label: str
    description: str
    purpose_text: str
    sensitive: bool = False


class ConsentApp(BaseModel):
    name: str
    logo_url: str | None = None
    publisher_kind: str
    publisher_name: str
    verified: bool = False
    homepage_url: str | None = None
    privacy_url: str | None = None


class ConsentDataFlow(BaseModel):
    direction: str
    entity: str
    fields: list[str] = Field(default_factory=list)
    purpose: str = ""
    destination_country: str | None = None


class ExistingAuthorization(BaseModel):
    scopes: list[str]
    granted_at: datetime


class AuthorizeView(BaseModel):
    request_id: str
    app_name: str
    scopes: list[str]
    resource: Resource
    # Epic 67 (additive): the consent screen payload; ``state`` = ok | app_not_installed | needs_admin_approval |
    # mcp_disabled (no redirect for those).
    state: str = "ok"
    app: ConsentApp | None = None
    scope_details: list[ConsentScope] = Field(default_factory=list)
    unavailable_scopes: list[ConsentScope] = Field(default_factory=list)
    not_held_scopes: list[ConsentScope] = Field(default_factory=list)
    data_flows: list[ConsentDataFlow] = Field(default_factory=list)
    existing_authorization: ExistingAuthorization | None = None


class AuthorizeDecision(BaseModel):
    request_id: str
    approve: bool
    scopes: list[str] | None = None


class AuthorizeRedirect(BaseModel):
    redirect_to: str


class IntrospectionOut(BaseModel):
    active: bool
    scope: str | None = None
    client_id: str | None = None
    exp: int | None = None
    sub: str | None = None
    aud: str | None = None
    # Epic 67 (additive, RFC 7662)
    token_type: str | None = None
    iat: int | None = None
    iss: str | None = None
    tenant: str | None = None
