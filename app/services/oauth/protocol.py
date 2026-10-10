# ruff: noqa: D401, E501
"""OAuth 2.1 protocol endpoints (Epic 67, stories 67.2-67.4; spec 0.2-0.5), with the frozen 66 signatures.

* ``token``: ``client_credentials`` (tenant slug, active Installation, resource ``api``, no sensitive scope -- D-7),
  ``authorization_code`` (``authorize.exchange_code``) and ``refresh_token`` (``refresh.refresh``). Every other grant ->
  ``unsupported_grant_type``. Errors are ``OAuthError`` (RFC 6749 shape, rendered by ``router/oauth.py``).
* ``revoke_token`` (RFC 7009): always succeeds; only the authenticated client's own tokens are touched (a refresh token
  revokes its family, an access token only itself).
* ``introspect`` (RFC 7662): ``{active: false}`` for unknown, expired, revoked, rotated or foreign tokens.
* ``metadata`` (RFC 8414): no ``registration_endpoint`` (D-9), S256 only.
"""

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import DomainError
from app.core.oauth_scopes import scopes as scope_catalogue
from app.core.settings import get_settings
from app.models.identity import Tenant
from app.models.oauth import App, AppInstallation, OAuthToken, OAuthTokenLookup
from app.schemas.oauth import (
    AuthorizeDecision,
    AuthorizeRedirect,
    AuthorizeRequest,
    AuthorizeView,
    ClientAuth,
    IntrospectionOut,
    IssuedTokens,
    TokenRequest,
    TokenResponse,
)
from app.services import ecosystem_service
from app.services.oauth import authorize as _authorize
from app.services.oauth.client_auth import AuthedClient, OAuthError, authenticate_client, invalid_client
from app.services.oauth.principals import clear_cache
from app.services.oauth.refresh import refresh
from app.services.oauth.tokens import USABLE_INSTALLATION_STATUSES, hash_token, issue_token, revoke_for


def _now() -> datetime:
    return datetime.now(UTC)


def _response(issued: IssuedTokens) -> TokenResponse:
    return TokenResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        refresh_token=issued.refresh_token,
        scope=" ".join(issued.scopes),
    )


async def _client_credentials(client: AuthedClient, form: TokenRequest) -> IssuedTokens:
    if client.client_type != "confidential" or "client_credentials" not in client.grant_types:
        raise OAuthError("unauthorized_client", "This client may not use client credentials")
    if not form.tenant:
        raise OAuthError("invalid_request", "tenant is required")
    if form.resource:
        key = _authorize.resource_key(form.resource)
        if key != "api":
            raise OAuthError("invalid_target", "Client credentials tokens are only for the Public API")
    async with platform_session() as pdb:
        tenant = (await pdb.execute(select(Tenant).where(Tenant.slug == form.tenant.strip().lower()))).scalar_one_or_none()
        tenant_id = tenant.id if tenant is not None and tenant.status == "active" else None
    if tenant_id is None:
        raise OAuthError("invalid_request", "Unknown tenant")
    state = await ecosystem_service.is_suspended(tenant_id, app_id=client.app_id)
    if state.suspended:
        if state.scope == "platform_app":
            raise invalid_client("The app is suspended")
        raise OAuthError("ecosystem_suspended", "External access to this workspace is suspended", status=403)
    catalogue = scope_catalogue()
    async with tenant_session(tenant_id) as db:
        inst = (
            await db.execute(
                select(AppInstallation).where(
                    AppInstallation.kind == "app", AppInstallation.app_id == client.app_id,
                    AppInstallation.status.in_(USABLE_INSTALLATION_STATUSES),
                )
            )
        ).scalar_one_or_none()  # fmt: skip
        if inst is None:
            raise invalid_client("The app is not installed in this workspace")
        granted = list(inst.granted_scopes or [])
        if form.scope and form.scope.strip():
            wanted = sorted(set(form.scope.split()))
            for key in wanted:
                spec = catalogue.get(key)
                if spec is None:
                    raise OAuthError("invalid_scope", f"Unknown scope {key}")
                if key.startswith("mcp:"):
                    raise OAuthError("invalid_target", "MCP scopes need a user's authorization")
                if spec.sensitive:
                    raise OAuthError("invalid_scope", f"Scope {key} needs a user's authorization")  # D-7
                if key == "offline_access":
                    raise OAuthError("invalid_scope", "Client credentials tokens have no refresh token")
                if key not in granted:
                    raise OAuthError("invalid_scope", f"Scope {key} is not granted to this installation")
        else:
            wanted = sorted(
                k for k in granted
                if k in catalogue and not catalogue[k].sensitive and not k.startswith("mcp:") and k != "offline_access"
                and "api" in catalogue[k].resources
            )  # fmt: skip
            if not wanted:
                raise OAuthError("invalid_scope", "No scope is granted for app access")
        try:
            return await issue_token(db, installation_id=inst.id, grant_type="client_credentials", resource="api", scopes=wanted)
        except DomainError as exc:
            if exc.code in ("app_not_installed", "app_unavailable"):
                raise invalid_client(exc.message) from exc
            raise OAuthError(exc.code if exc.code == "invalid_scope" else "invalid_request", exc.message) from exc


async def token(form: TokenRequest, client: ClientAuth) -> TokenResponse:
    authed = await authenticate_client(client)
    if form.grant_type == "client_credentials":
        issued = await _client_credentials(authed, form)
    elif form.grant_type == "authorization_code":
        issued = await _authorize.exchange_code(authed, code=form.code, redirect_uri=form.redirect_uri, code_verifier=form.code_verifier)
    elif form.grant_type == "refresh_token":
        issued = await refresh(authed, form.refresh_token, form.scope)
    else:  # pragma: no cover - the Literal refuses it earlier
        raise OAuthError("unsupported_grant_type", "Unsupported grant type")
    return _response(issued)


async def authorize_request(ctx: RequestContext, db: AsyncSession, req: AuthorizeRequest) -> AuthorizeView:
    return await _authorize.authorize_request(ctx, db, req)


async def authorize_decision(ctx: RequestContext, db: AsyncSession, req: AuthorizeDecision) -> AuthorizeRedirect:
    return await _authorize.authorize_decision(ctx, db, req)


async def _own_token(token_: str, client: AuthedClient) -> tuple[OAuthTokenLookup, OAuthToken, AppInstallation] | None:
    """The token row when it belongs to ``client`` (else None; nothing is revealed for foreign tokens)."""
    digest = hash_token(token_)
    async with platform_session() as pdb:
        lookup = await pdb.get(OAuthTokenLookup, digest)
    if lookup is None or lookup.kind not in ("access", "refresh"):
        return None
    async with tenant_session(lookup.tenant_id) as db:
        row = await db.get(OAuthToken, lookup.token_id)
        inst = await db.get(AppInstallation, row.installation_id) if row is not None else None
    if row is None or inst is None or inst.app_id != client.app_id:
        return None
    return lookup, row, inst


async def revoke_token(token: str, client: ClientAuth) -> None:
    authed = await authenticate_client(client)
    found = await _own_token(token, authed) if token else None
    if found is None:
        return
    lookup, row, _ = found
    async with tenant_session(lookup.tenant_id) as db:
        if row.kind == "refresh":
            await revoke_for(db, family_id=row.family_id, reason="revoked")
        else:
            await db.execute(
                update(OAuthToken).where(OAuthToken.id == row.id, OAuthToken.revoked_at.is_(None)).values(revoked_at=_now(), revoked_reason="revoked")
            )
    async with platform_session() as pdb:
        await pdb.execute(delete(OAuthTokenLookup).where(OAuthTokenLookup.token_hash == lookup.token_hash))
    clear_cache()


async def introspect(token: str, client: ClientAuth) -> IntrospectionOut:
    authed = await authenticate_client(client)
    found = await _own_token(token, authed) if token else None
    if found is None:
        return IntrospectionOut(active=False)
    lookup, row, inst = found
    now = _now()
    if row.revoked_at is not None or row.expires_at <= now or row.rotated_at is not None:
        return IntrospectionOut(active=False)
    if inst.status not in USABLE_INSTALLATION_STATUSES:
        return IntrospectionOut(active=False)
    async with platform_session() as pdb:
        tenant = await pdb.get(Tenant, lookup.tenant_id)
        app = await pdb.get(App, authed.app_id)
    if app is None or app.status in ("suspended", "retired"):
        return IntrospectionOut(active=False)
    return IntrospectionOut(
        active=True,
        scope=" ".join(row.scopes),
        client_id=authed.client_id,
        token_type="refresh_token" if row.kind == "refresh" else "access_token",
        exp=int(row.expires_at.timestamp()),
        iat=int(row.created_at.timestamp()),
        sub=str(row.user_id or row.installation_id),
        aud=_authorize.resource_url(row.resource),
        iss=get_settings().p5_oauth_issuer(),
        tenant=str(tenant.slug) if tenant is not None else None,
    )


def metadata() -> dict[str, object]:
    """RFC 8414 authorization server metadata (no ``registration_endpoint``: D-9)."""
    issuer = get_settings().p5_oauth_issuer()
    catalogue = scope_catalogue()
    supported = sorted(k for k, spec in catalogue.items() if not spec.sensitive)
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "revocation_endpoint": f"{issuer}/oauth/revoke",
        "introspection_endpoint": f"{issuer}/oauth/introspect",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
        "revocation_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
        "introspection_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
        "scopes_supported": supported,
        "resource_indicators_supported": True,
        "service_documentation": f"{issuer}/developers/docs/auth",
    }


__all__ = ["OAuthError", "authorize_decision", "authorize_request", "introspect", "metadata", "revoke_token", "token"]
