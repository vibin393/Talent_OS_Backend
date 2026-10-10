"""FastAPI authentication dependencies (identity story 1.3 + Epic 12 combined principal resolution).

Tenant-user routes (story 1.3):

* ``current_user`` / ``CurrentUserDep``: authenticates the ``tos_session`` cookie (session -> user; tenant
  active, user active, idle/absolute expiry; CSRF on mutations) and puts the principal and ``tenant_id`` on
  the request context.
* ``get_tenant_db`` / ``TenantDBSessionDep``: a DB session RLS-scoped to the authenticated user's tenant.
  It depends on ``current_user``, so it can never resolve before authentication and silently fall back to
  a platform session (unlike the context-based ``app.core.database.get_db``). Authenticated tenant routes
  must use ``TenantDBSessionDep``.

Combined principal resolution for tenant routes (Epic 12, AD-10), in order:

1. ``Authorization: Bearer tos_...`` -> ``ApiKeyPrincipal`` (``type="api_key"``): no CSRF, cookies ignored.
2. ``tos_support`` cookie -> ``SupportPrincipal`` (``type="support"``): every ``*:read`` at tenant scope
   except ``compensation:view`` / ``*:export``; grant re-checked on every request
   (401 ``support_access_ended``); read-only (403 ``support_read_only``); each request pre-audited as
   ``support.request``.
3. ``tos_session`` cookie -> the identity user principal. Offboarding tenants admit tenant admins only,
   read-only (403 ``tenant_read_only`` on writes).

``install()`` registers the combined resolver via ``set_principal_hook`` (called from ``create_app``).
The resolved principal is cached on ``request.state`` like the identity resolver does.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import idempotency, rate_limit, rate_limit_policies
from app.core.database import tenant_session
from app.core.errors import Forbidden, Unauthorized
from app.core.permissions import Principal, set_principal_resolver
from app.services import api_keys_service, support_access_service
from app.services import auth_service as service

PrincipalHook = Callable[[Request], Awaitable[Principal]]
_principal_hook: PrincipalHook | None = None


def set_principal_hook(hook: PrincipalHook | None) -> None:
    """Epic 12: ``install()`` registers the combined resolver (API key bearer, support session, then the
    user cookie) so every tenant route -- incl. ``TenantDBSessionDep`` -- accepts those principals."""
    global _principal_hook  # process-wide wiring, set once at startup
    _principal_hook = hook


async def current_user(request: Request) -> Principal:
    """Return the authenticated principal (permissions, teams, BU resolved; cached per request)."""
    if _principal_hook is not None:
        return await _principal_hook(request)
    return await service.authenticate_user(request)


# ``app.core.permissions.require(...)`` authenticates through this resolver (core never imports modules).
set_principal_resolver(current_user)

CurrentUserDep = Annotated[Principal, Depends(current_user)]


async def get_tenant_db(principal: CurrentUserDep) -> AsyncIterator[AsyncSession]:
    if principal.tenant_id is None:  # defensive: user principals always carry a tenant
        raise Unauthorized()
    async with tenant_session(principal.tenant_id) as session:
        if principal.read_only:
            # Epic 12: support access / offboarding sessions can never write (DB-level guard as well).
            await session.execute(text("SET TRANSACTION READ ONLY"))
        yield session


TenantDBSessionDep = Annotated[AsyncSession, Depends(get_tenant_db)]

# Epic 13: ``core.idempotency`` guards share the route's tenant session (same dependency callable).
idempotency.set_session_dependency(get_tenant_db)


# ================================================================ combined resolver (Epic 12)
BEARER_PREFIX = "bearer "
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOGOUT_PATHS = frozenset({"/api/v1/auth/logout"})


OAUTH_ACCESS_PREFIX = "tos_at_"
PRINCIPAL_STATE_ATTR_PUBLIC = "tos_public_principal"
_OAUTH_PREFIXES = ("tos_at_", "tos_rt_", "tos_ac_", "tos_cs_")


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header[: len(BEARER_PREFIX)].lower() == BEARER_PREFIX:
        return header[len(BEARER_PREFIX) :].strip()
    return None


def bearer_key(request: Request) -> str | None:
    """A ``tos_`` API key bearer (Epic 12). OAuth artefacts (``tos_at_`` ...) are not API keys."""
    token = _bearer(request)
    if token is not None and token.startswith("tos_") and not token.startswith(_OAUTH_PREFIXES):
        return token
    return None


def bearer_oauth(request: Request) -> str | None:
    """Epic 66 (AD-76): an OAuth access token bearer (``tos_at_``)."""
    token = _bearer(request)
    return token if token is not None and token.startswith(OAUTH_ACCESS_PREFIX) else None


def app_token_not_allowed() -> Forbidden:
    return Forbidden(
        "App tokens are accepted only on the Public API (/public/v1) and MCP", code="app_token_not_allowed"
    )


async def resolve_principal(request: Request) -> Principal:
    cached = getattr(request.state, service.PRINCIPAL_STATE_ATTR, None)
    if isinstance(cached, Principal):
        return cached
    raw = _bearer(request)
    if raw is not None and raw.startswith(_OAUTH_PREFIXES):
        # D-2 (AD-77): the internal /api/v1 surface never accepts App tokens.
        raise app_token_not_allowed()
    key = bearer_key(request)
    if key is not None:
        principal = await api_keys_service.authenticate(request, key)
    elif request.cookies.get(support_access_service.SUPPORT_COOKIE) and not request.cookies.get(
        "tos_session"
    ):
        principal = await support_access_service.resolve_support_principal(
            request, request.cookies[support_access_service.SUPPORT_COOKIE]
        )
    else:
        principal = await service.authenticate_user(request)
        if (
            principal.read_only
            and request.method.upper() not in READ_METHODS
            and request.url.path not in LOGOUT_PATHS
        ):
            raise Forbidden(
                "This workspace is read-only while it is being offboarded", code="tenant_read_only"
            )
    setattr(request.state, service.PRINCIPAL_STATE_ATTR, principal)
    # NFR-16 per-user / per-tenant limits (API keys carry their own per-key limit as well).
    if principal.type != "api_key":
        await rate_limit.enforce("user", str(principal.id), *rate_limit_policies.USER_LIMIT)
    if principal.tenant_id is not None:
        await rate_limit.enforce("tenant", str(principal.tenant_id), *rate_limit_policies.TENANT_LIMIT)
    if principal.type == "user":
        # Epic 11 (SM-3): at most one ``user_active_days`` row per user per day; never fails the request.
        from app.services import reporting_service

        await reporting_service.touch_active_day(principal)
    return principal


def install() -> None:
    set_principal_hook(resolve_principal)
