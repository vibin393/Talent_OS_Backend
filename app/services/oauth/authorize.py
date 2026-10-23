# ruff: file-ignore[line-too-long]
"""Authorize + consent (Epic 67, story 67.3; spec 0.4) and the code exchange.

* ``check_query`` validates the raw RFC 6749 query in order: client and exact redirect (failure -> error page, never
  a redirect), then response type / PKCE S256 / state (failure -> redirect with ``error=...&state=...``).
* ``authorize_request`` (session user) resolves the Installation (else ``app_not_installed``), splits the requested
  scopes into approvable / ``unavailable_scopes`` (outside the Grant) / ``not_held_scopes`` (the user lacks the mapped
  permissions), applies ``app_policy.user_self_authorize`` and the MCP pre-checks, and caches the validated request
  (``oauth:authz:<request_id>``, 10 min) so the decision cannot change client, redirect or PKCE values.
* ``authorize_decision`` (★: the user's own click) is single-use, bound to the user; approve records the authorisation
  (union with a live one), inserts a 60 s single-use code and returns the redirect.
* ``exchange_code`` (token endpoint, ``authorization_code``): PKCE verify, byte-equal redirect, single use; a replayed
  code revokes every token issued from it.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import DomainError, Forbidden, ValidationFailed
from app.core.oauth_scopes import get_scope
from app.core.oauth_scopes import scopes as scope_catalogue
from app.core.permissions import CATALOG, Principal
from app.core.settings import get_settings
from app.models.ecosystem import DEFAULT_APP_POLICY
from app.models.identity import Tenant
from app.models.oauth import (
    App,
    AppInstallation,
    AppUserAuthorization,
    OAuthAuthCode,
    OAuthToken,
    OAuthTokenLookup,
    PartnerOrg,
)
from app.schemas.oauth import (
    AuthorizeDecision,
    AuthorizeRedirect,
    AuthorizeRequest,
    AuthorizeView,
    ConsentApp,
    ConsentDataFlow,
    ConsentScope,
    ExistingAuthorization,
    IssuedTokens,
)
from app.services import ecosystem_service, entity_refs
from app.services.oauth.authorizations import record_user_authorization
from app.services.oauth.client_auth import AuthedClient, OAuthError
from app.services.oauth.tokens import CODE_PREFIX, hash_token, issue_token, new_token, revoke_for

INSTALLED_STATUSES = frozenset({"active", "degraded", "reconsent_required"})
PKCE_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
CACHE_PREFIX = "oauth:authz:"


def _now() -> datetime:
    return datetime.now(UTC)


class AuthorizePageError(DomainError):
    """No redirect: the consent page shows "This link isn't valid" (unknown client or redirect mismatch)."""

    status_code = 400
    code = "invalid_redirect_uri"
    default_message = "This link isn't valid. Return to the app and try again."


# ================================================================ helpers (pure)
def build_redirect(uri: str, **params: str | None) -> str:
    """Append params with proper encoding; an existing query is preserved (never an open redirect: ``uri`` is one of
    the App's registered URIs)."""
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True) + [(k, v) for k, v in params.items() if v is not None]
    return urlunsplit(parts._replace(query=urlencode(query)))


def pkce_s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def resource_key(raw: str | None) -> str | None:
    s = get_settings()
    if raw is None or raw == "" or raw == "api" or raw.rstrip("/") == s.p5_public_api_base_url():
        return "api"
    if raw == "mcp" or raw.rstrip("/") == s.p5_mcp_resource_url():
        return "mcp"
    return None


def resource_url(key: str) -> str:
    s = get_settings()
    return s.p5_mcp_resource_url() if key == "mcp" else s.p5_public_api_base_url()


def _consent_scope(key: str) -> ConsentScope:
    spec = get_scope(key)
    label = "Stay connected when you're not using the app" if key == "offline_access" else spec.label
    return ConsentScope(key=key, label=label, description=spec.description, purpose_text=spec.purpose_text, sensitive=spec.sensitive)


# ================================================================ request cache (Redis; in-process when memory)
_memory: dict[str, tuple[str, float]] = {}


def _use_memory() -> bool:
    return get_settings().rate_limit_backend == "memory"


async def _redis() -> Any:
    from redis.asyncio import Redis

    s = get_settings()
    return Redis(host=s.redis_host, port=s.redis_port, db=2, password=s.redis_password or None, decode_responses=True,
                 socket_connect_timeout=0.5, socket_timeout=0.5)  # fmt: skip


async def _put(request_id: str, data: dict[str, Any]) -> None:
    ttl = get_settings().oauth_authorize_request_ttl_seconds
    blob = json.dumps(data, default=str)
    if _use_memory():
        _memory[request_id] = (blob, time.monotonic() + ttl)
        return
    try:
        client = await _redis()
        await client.set(CACHE_PREFIX + request_id, blob, ex=ttl)
        await client.aclose()
    except Exception as exc:
        raise _unavailable() from exc


async def _pop(request_id: str) -> dict[str, Any] | None:
    if _use_memory():
        hit = _memory.pop(request_id, None)
        return json.loads(hit[0]) if hit is not None and hit[1] > time.monotonic() else None
    try:
        client = await _redis()
        blob = await client.getdel(CACHE_PREFIX + request_id)
        await client.aclose()
    except Exception as exc:
        raise _unavailable() from exc
    return json.loads(blob) if blob else None


def _unavailable() -> DomainError:
    exc = DomainError("Sign-in is temporarily unavailable", code="temporarily_unavailable")
    exc.status_code = 503
    return exc


# ================================================================ query validation
async def check_query(query: Mapping[str, str]) -> AuthorizeRequest | AuthorizeRedirect:
    """Validate the raw query (spec 0.4 steps 2-3). Returns the request, or a redirect carrying the error."""
    client_id = (query.get("client_id") or "").strip()
    redirect_uri = query.get("redirect_uri") or ""
    async with platform_session() as pdb:
        app = (await pdb.execute(select(App).where(App.client_id == client_id))).scalar_one_or_none() if client_id else None
        uris = list(app.redirect_uris or []) if app is not None else []
    if app is None or redirect_uri not in uris:
        raise AuthorizePageError()
    state = query.get("state")

    def err(error: str, description: str) -> AuthorizeRedirect:
        return AuthorizeRedirect(redirect_to=build_redirect(redirect_uri, error=error, error_description=description, state=state))

    if query.get("response_type") != "code":
        return err("unsupported_response_type", "Only response_type=code is supported")
    if "authorization_code" not in (app.grant_types or []):
        return err("unauthorized_client", "This client may not use the authorization code flow")
    if not state or len(state) > 512:
        return err("invalid_request", "state is required")
    challenge = query.get("code_challenge") or ""
    if query.get("code_challenge_method") != "S256" or not PKCE_RE.fullmatch(challenge):
        return err("invalid_request", "PKCE S256 is required")
    if not (query.get("scope") or "").strip():
        return err("invalid_scope", "scope is required")
    return AuthorizeRequest(
        client_id=client_id, redirect_uri=redirect_uri, scope=query["scope"], state=state, code_challenge=challenge,
        code_challenge_method="S256", resource=query.get("resource") or None,
    )  # fmt: skip


def _user(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user" or ctx.tenant_id is None:
        raise Forbidden("Sign in to continue", code="human_session_required")
    return principal


async def _publisher(pdb: AsyncSession, app: App) -> tuple[str, bool]:
    if app.publisher_kind == "platform":
        return "Talent OS", True
    if app.publisher_kind == "partner" and app.partner_org_id:
        org = await pdb.get(PartnerOrg, app.partner_org_id)
        return (org.name if org else "Partner"), bool(org and org.verification_status == "verified")
    tenant = await pdb.get(Tenant, app.owner_tenant_id) if app.owner_tenant_id else None
    return (tenant.name if tenant is not None else "Your organisation"), False


async def authorize_request(ctx: RequestContext, db: AsyncSession, req: AuthorizeRequest) -> AuthorizeView:
    user = _user(ctx)
    tenant_id = ctx.tenant_id
    assert tenant_id is not None
    async with platform_session() as pdb:
        app = (await pdb.execute(select(App).where(App.client_id == req.client_id))).scalar_one_or_none()
        if app is None or req.redirect_uri not in (app.redirect_uris or []):
            raise AuthorizePageError()
        publisher_name, verified = await _publisher(pdb, app)
        app_id, app_name, app_status = app.id, app.name, app.status
        resources, manifest = list(app.resources), dict(app.manifest or {})
        consent_app = ConsentApp(name=app.name, publisher_kind=app.publisher_kind, publisher_name=publisher_name,
                                 verified=verified, homepage_url=app.homepage_url, privacy_url=app.privacy_url)  # fmt: skip

    def redirect_error(error: str, description: str) -> OAuthRedirectError:
        return OAuthRedirectError(build_redirect(req.redirect_uri, error=error, error_description=description, state=req.state))

    resource = resource_key(req.resource)
    if resource is None or resource not in resources:
        raise redirect_error("invalid_target", "The requested resource is not available to this app")
    requested = sorted(set(req.scope.split()))
    catalogue = scope_catalogue()
    unknown = [k for k in requested if k not in catalogue]
    if unknown:
        raise redirect_error("invalid_scope", f"Unknown scope {unknown[0]}")
    has_mcp = any(k.startswith("mcp:") for k in requested)
    if has_mcp and resource != "mcp":
        raise redirect_error("invalid_target", "mcp:* scopes need the MCP resource")
    if resource == "mcp" and not has_mcp:
        raise redirect_error("invalid_scope", "The MCP resource needs an mcp:* scope")
    base = AuthorizeView(request_id="", app_name=app_name, scopes=[], resource=resource, app=consent_app)  # type: ignore[arg-type]
    inst = (
        await db.execute(
            select(AppInstallation).where(
                AppInstallation.kind == "app", AppInstallation.app_id == app_id, AppInstallation.status.in_(INSTALLED_STATUSES)
            )
        )
    ).scalar_one_or_none()
    if inst is None or app_status in ("suspended", "retired"):
        return base.model_copy(update={"state": "app_not_installed"})
    granted = set(inst.granted_scopes or [])
    unavailable = [k for k in requested if k not in granted]
    candidates = [k for k in requested if k in granted]
    not_held = [k for k in candidates if not all(user.has(p) for p in get_scope(k).permissions)]
    approvable = [k for k in candidates if k not in not_held]
    existing = (
        await db.execute(
            select(AppUserAuthorization).where(
                AppUserAuthorization.installation_id == inst.id, AppUserAuthorization.user_id == user.id,
                AppUserAuthorization.resource == resource, AppUserAuthorization.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()  # fmt: skip
    eco = await ecosystem_service.settings_row(db, tenant_id)
    policy = {**DEFAULT_APP_POLICY, **(eco.app_policy or {})}
    view = base.model_copy(
        update={
            "scopes": approvable,
            "scope_details": [_consent_scope(k) for k in approvable],
            "unavailable_scopes": [_consent_scope(k) for k in unavailable],
            "not_held_scopes": [_consent_scope(k) for k in not_held],
            "data_flows": [
                ConsentDataFlow(direction=f.get("direction", ""), entity=f.get("entity", ""), fields=list(f.get("fields", [])),
                                purpose=f.get("purpose", ""), destination_country=f.get("destination_country"))
                for f in manifest.get("data_flows", []) if isinstance(f, dict)
            ],
            "existing_authorization": ExistingAuthorization(scopes=list(existing.scopes), granted_at=existing.granted_at) if existing else None,
        }
    )  # fmt: skip
    if not policy.get("user_self_authorize", True) and existing is None:
        return view.model_copy(update={"state": "needs_admin_approval"})
    if resource == "mcp":
        if eco.mcp_enabled_at is None:
            return view.model_copy(update={"state": "mcp_disabled"})
        if "mcp:use" in CATALOG and not user.has("mcp:use"):
            return view.model_copy(update={"state": "mcp_disabled"})
    request_id = secrets.token_urlsafe(24)
    await _put(
        request_id,
        {"tenant_id": str(tenant_id), "user_id": str(user.id), "app_id": str(app_id), "installation_id": str(inst.id),
         "client_id": req.client_id, "redirect_uri": req.redirect_uri, "state": req.state,
         "code_challenge": req.code_challenge, "resource": resource, "approvable": approvable},
    )  # fmt: skip
    return view.model_copy(update={"request_id": request_id})


class OAuthRedirectError(DomainError):
    """An authorize error delivered to the client as a redirect (``redirect_to`` in the body)."""

    status_code = 400
    code = "oauth_redirect"

    def __init__(self, redirect_to: str) -> None:
        super().__init__("Returning the error to the app", details={"redirect_to": redirect_to})
        self.redirect_to = redirect_to


async def authorize_decision(ctx: RequestContext, db: AsyncSession, req: AuthorizeDecision) -> AuthorizeRedirect:
    require_human(actor_from_ctx(ctx))  # ★ the user's own click
    user = _user(ctx)
    data = await _pop(req.request_id)
    if data is None or data.get("user_id") != str(user.id) or data.get("tenant_id") != str(ctx.tenant_id):
        raise Forbidden("This sign-in request is no longer valid. Return to the app and try again.", code="invalid_request")
    redirect_uri, state = data["redirect_uri"], data["state"]
    installation_id = uuid.UUID(data["installation_id"])
    approvable = list(data["approvable"])
    if not req.approve:
        audit.record("oauth.consent_denied", "app_installation", installation_id, session=db, app_id=data["app_id"])
        return AuthorizeRedirect(redirect_to=build_redirect(redirect_uri, error="access_denied", state=state))
    chosen = sorted(set(req.scopes if req.scopes is not None else approvable))
    if not chosen:
        raise ValidationFailed("Choose at least one permission", details={"fields": {"scopes": "Required"}})
    if not set(chosen) <= set(approvable):
        raise ValidationFailed("You can only allow the listed permissions", details={"fields": {"scopes": "Not allowed"}})
    assert ctx.tenant_id is not None
    state_ = await ecosystem_service.is_suspended(ctx.tenant_id, app_id=uuid.UUID(data["app_id"]))
    if state_.suspended:
        raise Forbidden("External access is paused by your organisation", code="ecosystem_suspended")
    resource = data["resource"]
    existing = (
        await db.execute(
            select(AppUserAuthorization.scopes).where(
                AppUserAuthorization.installation_id == installation_id, AppUserAuthorization.user_id == user.id,
                AppUserAuthorization.resource == resource, AppUserAuthorization.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()  # fmt: skip
    union = sorted(set(existing or []) | set(chosen))
    await record_user_authorization(db, installation_id=installation_id, user_id=user.id, scopes=union, resource=resource)
    code = new_token(CODE_PREFIX)
    expires = _now() + timedelta(seconds=get_settings().oauth_code_ttl_seconds)
    row = OAuthAuthCode(
        id=uuid.uuid4(), tenant_id=ctx.tenant_id, installation_id=installation_id, user_id=user.id, code_hash=hash_token(code),
        redirect_uri=redirect_uri, scopes=chosen, resource=resource, code_challenge=data["code_challenge"],
        code_challenge_method="S256", expires_at=expires,
    )  # fmt: skip
    db.add(row)
    await db.flush()
    async with platform_session() as pdb:
        pdb.add(OAuthTokenLookup(token_hash=row.code_hash, tenant_id=ctx.tenant_id, token_id=row.id, kind="code",
                                 expires_at=expires + timedelta(hours=1)))  # fmt: skip
    audit.record("oauth.consent_granted", "app_installation", installation_id, session=db, scopes=chosen, resource=resource)
    return AuthorizeRedirect(redirect_to=build_redirect(redirect_uri, code=code, state=state))


# ================================================================ code exchange (token endpoint)
async def _revoke_issued_from(tenant_id: uuid.UUID, code_id: uuid.UUID) -> None:
    async with tenant_session(tenant_id) as db:
        families = set((await db.execute(select(OAuthToken.family_id).where(OAuthToken.issued_from_code_id == code_id))).scalars())
        for fam in families:
            await revoke_for(db, family_id=fam, reason="reuse_detected")


async def exchange_code(client: AuthedClient, *, code: str | None, redirect_uri: str | None, code_verifier: str | None) -> IssuedTokens:
    if "authorization_code" not in client.grant_types:
        raise OAuthError("unauthorized_client", "This client may not use the authorization code flow")
    if not code or not redirect_uri or not code_verifier:
        raise OAuthError("invalid_request", "code, redirect_uri and code_verifier are required")
    async with platform_session() as pdb:
        lookup = await pdb.get(OAuthTokenLookup, hash_token(code))
        tenant_id, code_id = (lookup.tenant_id, lookup.token_id) if lookup is not None and lookup.kind == "code" else (None, None)
    if tenant_id is None or code_id is None:
        raise OAuthError("invalid_grant", "The code is invalid or expired")
    now = _now()
    replayed = False
    error: OAuthError | None = None
    issued: IssuedTokens | None = None
    async with tenant_session(tenant_id) as db:
        row = (await db.execute(select(OAuthAuthCode).where(OAuthAuthCode.id == code_id).with_for_update())).scalar_one_or_none()
        inst = await db.get(AppInstallation, row.installation_id) if row is not None else None
        if row is None or inst is None:
            error = OAuthError("invalid_grant", "The code is invalid or expired")
        elif row.consumed_at is not None:
            replayed, error = True, OAuthError("invalid_grant", "The code was already used")
        elif row.expires_at <= now:
            error = OAuthError("invalid_grant", "The code expired")
        elif inst.app_id != client.app_id:
            error = OAuthError("invalid_grant", "The code was not issued to this client")
        elif not hmac.compare_digest(row.redirect_uri.encode(), redirect_uri.encode()):
            error = OAuthError("invalid_grant", "redirect_uri does not match")
        elif not PKCE_RE.fullmatch(code_verifier) or not hmac.compare_digest(pkce_s256(code_verifier), row.code_challenge):
            error = OAuthError("invalid_grant", "PKCE verification failed")
        elif await entity_refs.principal_for_user(db, tenant_id, row.user_id) is None:
            error = OAuthError("invalid_grant", "The user is no longer active")
        else:
            auth = (
                await db.execute(
                    select(AppUserAuthorization).where(
                        AppUserAuthorization.installation_id == inst.id, AppUserAuthorization.user_id == row.user_id,
                        AppUserAuthorization.resource == row.resource, AppUserAuthorization.revoked_at.is_(None),
                    )
                )
            ).scalar_one_or_none()  # fmt: skip
            if auth is None:
                error = OAuthError("invalid_grant", "The authorization was revoked")
            else:
                row.consumed_at = now
                await db.flush()
                try:
                    issued = await issue_token(
                        db, installation_id=inst.id, grant_type="authorization_code",
                        resource=row.resource,  # type: ignore[arg-type]
                        scopes=[k for k in row.scopes if k in set(auth.scopes)], user_id=row.user_id,
                        user_authorization_id=auth.id, with_refresh="offline_access" in row.scopes, issued_from_code_id=row.id,
                    )  # fmt: skip
                except DomainError as exc:
                    raise OAuthError("invalid_grant", exc.message) from exc
    if replayed:
        await _revoke_issued_from(tenant_id, code_id)
    if error is not None:
        raise error
    assert issued is not None
    return issued


__all__ = [
    "AuthorizePageError",
    "OAuthRedirectError",
    "authorize_decision",
    "authorize_request",
    "build_redirect",
    "check_query",
    "exchange_code",
    "pkce_s256",
    "resource_key",
    "resource_url",
]
