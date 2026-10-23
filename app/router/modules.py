"""Module gateway — the single backend surface for the unified shell (native unification).

The unified Talent OS frontend is becoming the one and only UI (Frappe-style): other products
(Croar, Feedback IQ, Portal) are rendered as *native* pages inside it, not iframes. Those native
pages still need each product's data, so the shell calls ONE backend surface — this gateway — at
``/api/v1/modules/{module}/{path}``.

For each request the gateway:
  1. Authenticates the Talent OS session (``CurrentUserDep``) and reads the user's email/name.
  2. Checks the tenant's plan ENTITLES the requested module (403 ``module_not_entitled`` otherwise).
  3. Exchanges that identity for the target module's OWN session token, server-to-server, by minting
     a central tenant-claim JWT — carrying the user's effective ``module_caps`` (unified RBAC) — and
     POSTing it to the module's ``central-sso`` endpoint. The module token is cached per
     (user, tenant, module, caps) and re-minted on expiry (or a 401 from the module).
  4. Reverse-proxies the request to the module's backend with that bearer token and streams the
     response back.

Security posture (hardening):
  * The user's REAL module caps are minted into the central token — an embedded native page therefore
    gets exactly the caps the user holds, never blanket admin.
  * Inbound client headers are dropped except a small ALLOW-LIST — spoofable identity/tenant headers
    (``x-api-key``, ``x-forwarded-*``, ``x-user-*``, ``x-tenant-*``, …) never reach the module.
  * The ``{path}`` is validated (no traversal, control chars, backslashes or encoded slashes) and the
    final upstream URL is asserted to stay under the module's configured prefix.
  * Upstream timeouts / connection failures map to a clear 502/504, not a misleading 401/500.

This keeps module tokens server-side (never in the browser), avoids per-subdomain CORS, and lets the
native pages use the normal same-origin ``/api/...`` client.
"""

import hashlib
import json
import os
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Request, Response

from app.core.errors import DomainError, Forbidden, Unauthorized
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.models.identity import User
from app.services import access_service as access
from app.services import central_token, entitlements

router = APIRouter(prefix="/modules", tags=["modules"])


class ModuleUnavailable(DomainError):
    """The module's backend could not be reached (connection refused / reset)."""

    status_code = 502
    code = "module_unavailable"
    default_message = "The module is unavailable"


class ModuleTimeout(DomainError):
    """The module's backend did not respond in time."""

    status_code = 504
    code = "module_timeout"
    default_message = "The module did not respond in time"


def _upstream_error(exc: Exception) -> DomainError:
    """Map an httpx transport failure to a clear gateway error (504 on timeout, else 502)."""
    return ModuleTimeout() if isinstance(exc, httpx.TimeoutException) else ModuleUnavailable()


# module -> {base URL (compose DNS), its central-sso path, its API prefix}
_MODULES: dict[str, dict[str, str]] = {
    "croar": {
        "base": os.getenv("CROAR_API_URL", "http://croar-backend:8000"),
        "sso": "/api/v1/auth/central-sso",
        "prefix": "/api/v1",
    },
    "feedbackiq": {
        "base": os.getenv("FEEDBACKIQ_API_URL", "http://feedbackiq-backend:8000"),
        "sso": "/api/central-sso",
        "prefix": "/api",
    },
    "portal": {
        "base": os.getenv("PORTAL_API_URL", "http://portal-backend:8000"),
        "sso": "/api/central-sso",
        "prefix": "/api",
    },
}

# Gateway module -> the entitlement module key (``app.services.entitlements.MODULES``) its plan must
# include. A gateway module with NO mapping fails closed (403): the plan can never be shown to include it.
_MODULE_ENTITLEMENT: dict[str, str] = {
    "croar": "intelligence",       # Croar's embedded AI features
    "feedbackiq": "experience",    # Feedback IQ engagement / surveys
    "portal": "talent_acquisition",  # the external Job Portal (hiring surface)
}

# {(user_id, tenant_id, module, caps_fingerprint): (module_token, expires_at_epoch)}.
# The caps fingerprint is part of the key so a role/caps change produces a cache MISS and a re-mint with
# the new caps (no stale, over-privileged token is ever replayed), and tenant_id prevents cross-tenant reuse.
_token_cache: dict[tuple[str, str, str, str], tuple[str, float]] = {}
_TOKEN_TTL = 240.0  # refreshed before the central token's 300s TTL

# ALLOW-LIST: the only inbound client headers forwarded upstream. Everything else is stripped, especially
# identity/tenant/proxy headers a browser (or a compromised page) could set to impersonate someone.
_ALLOWED_REQUEST_HEADERS = frozenset(
    {"content-type", "accept", "accept-language", "idempotency-key"}
)
# Hop-by-hop + body-framing response headers we must not pass back verbatim.
_DROP_RESPONSE_HEADERS = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "content-length", "content-encoding",
    }
)


def _caps_fingerprint(module_caps: dict[str, list[str]]) -> str:
    """A stable short hash of the user's effective module caps (canonical JSON)."""
    canonical = json.dumps(module_caps, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _safe_path(path: str) -> bool:
    """Reject anything that could escape the module's API prefix or smuggle a second path segment:
    traversal (``..``), control characters, backslashes, and percent-encoded dots/slashes."""
    if not path or any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        return False
    if "\\" in path or ".." in path:
        return False
    lowered = path.lower()
    return not any(token in lowered for token in ("%2e", "%2f", "%5c", "%00"))


async def _module_token(
    *, user_id: Any, tenant_id: Any, email: str | None, name: str | None,
    module_caps: dict[str, list[str]], caps_fp: str, module: str, cfg: dict[str, str],
    force: bool = False,
) -> str:
    """Return a cached (or freshly minted) module session token for this user, caps-aware."""
    key = (str(user_id), str(tenant_id), module, caps_fp)
    now = time.time()
    if not force:
        cached = _token_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]
    central = central_token.mint_central_token(
        user_id=user_id, tenant_id=tenant_id, email=email, name=name, module_caps=module_caps or None
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{cfg['base']}{cfg['sso']}", json={"token": central})
    except (httpx.TimeoutException, httpx.TransportError) as exc:  # module unreachable during SSO exchange
        raise _upstream_error(exc) from exc
    if resp.status_code != 200:
        raise Unauthorized()
    token = str(resp.json().get("access_token") or "")
    if not token:
        raise Unauthorized()
    _token_cache[key] = (token, now + _TOKEN_TTL)
    return token


@router.api_route("/{module}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def module_proxy(
    module: str, path: str, request: Request, principal: CurrentUserDep, db: TenantDBSessionDep
) -> Response:
    """Reverse-proxy a native-module request to its backend, authenticated as the current user."""
    cfg = _MODULES.get(module)
    if cfg is None or principal.tenant_id is None:
        raise Unauthorized()

    # Entitlement gate: the tenant's plan must include this module. Unknown gateway modules fail closed;
    # for a known module an entitlements outage keeps legacy behaviour (allow) rather than hard-failing.
    ent_module = _MODULE_ENTITLEMENT.get(module)
    if ent_module is None:
        raise Forbidden(f"module '{module}' is not entitled", code="module_not_entitled")
    try:
        entitled = await entitlements.tenant_has_module(principal.tenant_id, ent_module)
    except Exception:  # entitlements control-plane unavailable — don't break a known module
        entitled = True
    if not entitled:
        raise Forbidden(f"module '{module}' is not in this tenant's plan", code="module_not_entitled")

    if not _safe_path(path):
        raise Forbidden("Invalid module path", code="invalid_module_path")

    user = await db.get(User, principal.id)
    email = getattr(user, "email", None)
    name = getattr(user, "name", None)
    module_caps = await access.module_caps_for_user(db, principal.id)
    caps_fp = _caps_fingerprint(module_caps)

    body = await request.body()
    params = list(request.query_params.multi_items())
    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() in _ALLOWED_REQUEST_HEADERS
    }
    # Normalise the path and assert the final URL stays under the module's configured prefix.
    prefix = cfg["prefix"].rstrip("/")
    safe_segment = quote(path.lstrip("/"), safe="/-._~")
    url = f"{cfg['base']}{prefix}/{safe_segment}"
    if not url.startswith(f"{cfg['base']}{prefix}/"):  # defensive: never proxy outside the prefix
        raise Forbidden("Invalid module path", code="invalid_module_path")

    async def _send(token: str) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await client.request(
                    request.method, url, params=params, content=body,  # type: ignore[arg-type]
                    headers={**fwd_headers, "Authorization": f"Bearer {token}"},
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise _upstream_error(exc) from exc

    token = await _module_token(
        user_id=principal.id, tenant_id=principal.tenant_id, email=email, name=name,
        module_caps=module_caps, caps_fp=caps_fp, module=module, cfg=cfg,
    )
    upstream = await _send(token)
    if upstream.status_code == 401:  # module token expired/invalid — re-exchange once and retry
        token = await _module_token(
            user_id=principal.id, tenant_id=principal.tenant_id, email=email, name=name,
            module_caps=module_caps, caps_fp=caps_fp, module=module, cfg=cfg, force=True,
        )
        upstream = await _send(token)

    resp_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
