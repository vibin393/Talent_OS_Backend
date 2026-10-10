# ruff: noqa: E501
"""App Principals and Effective Rights (Epic 66, AD-75, spec 0.5).

* client credentials (resource ``api`` only): scopes = token.scopes ∩ installation.granted_scopes ∩ non-sensitive (D-7);
  permissions = ``permissions_for(scopes)`` at tenant scope.
* authorization code (``api`` or ``mcp``): the live user (active; tenant active) or 401; scopes = token.scopes ∩
  authorization.scopes ∩ granted; permissions = the user's own scope for each permission the scopes unlock; the user's
  teams and business unit (data scope = the user's).

``authenticate_bearer`` caches the resolved Principal per token for at most 60 s (NFR-61); ``revoke_for`` and every
installation / grant change clear the cache, so a deactivated user, a revoked authorisation and a suspended or
uninstalled Installation fail the next call within the window. Wrong resource -> 401 ``invalid_token``
(``wrong resource``); a suspended ecosystem -> 403 ``ecosystem_suspended``; a platform-suspended App -> 401.
"""

import time
import uuid  # noqa: TC003
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import Request
from sqlalchemy import update

from app.core.context import update_context
from app.core.database import platform_session, tenant_session
from app.core.errors import Forbidden, Unauthorized
from app.core.oauth_scopes import is_sensitive, permissions_for
from app.core.permissions import Principal, Scope
from app.models.oauth import AppInstallation, AppUserAuthorization, OAuthToken, OAuthTokenLookup
from app.services import ecosystem_service, entity_refs, identity_bridge
from app.services.oauth.tokens import ACCESS_PREFIX, USABLE_INSTALLATION_STATUSES, hash_token

CACHE_SECONDS = 60.0
LAST_USED_RESOLUTION = timedelta(minutes=1)
_cache: dict[bytes, tuple[Principal, float]] = {}


def clear_cache() -> None:
    _cache.clear()


def _now() -> datetime:
    return datetime.now(UTC)


def invalid_token(description: str = "invalid token") -> Unauthorized:
    exc = Unauthorized(
        "The access token is invalid or expired",
        code="invalid_token",
        details={"error_description": description},
    )

    def _hdr(response: object) -> None:
        headers = getattr(response, "headers", None)
        if headers is not None:
            headers["WWW-Authenticate"] = f'Bearer error="invalid_token", error_description="{description}"'

    exc.response_hooks.append(_hdr)  # type: ignore[arg-type]
    return exc


def ecosystem_suspended() -> Forbidden:
    return Forbidden("External access to this workspace is suspended", code="ecosystem_suspended")


def app_token_scopes(
    token_scopes: list[str], granted: list[str], auth_scopes: list[str] | None
) -> frozenset[str]:
    """Pure: the effective scope set of a token (delegated when ``auth_scopes`` is given)."""
    out = set(token_scopes) & set(granted)
    if auth_scopes is None:
        return frozenset(k for k in out if not is_sensitive(k))  # D-7
    return frozenset(out & set(auth_scopes))


def delegated_permissions(
    user_permissions: Mapping[str, Scope], scope_keys: frozenset[str]
) -> dict[str, Scope]:
    """Pure: the user's own scope for each permission the scopes unlock (never more than the user)."""
    wanted = permissions_for(scope_keys)
    return {p: Scope(user_permissions[p]) for p in wanted if p in user_permissions}


async def _resolve(token: str, resource: str) -> Principal:
    digest = hash_token(token)
    now = _now()
    async with platform_session() as pdb:
        lookup = await pdb.get(OAuthTokenLookup, digest)
        tenant_status = (
            await identity_bridge.tenant_status(pdb, lookup.tenant_id) if lookup is not None else None
        )
    if lookup is None or lookup.kind != "access" or lookup.expires_at <= now:
        raise invalid_token()
    if tenant_status != "active":
        raise invalid_token("workspace unavailable")
    async with tenant_session(lookup.tenant_id) as db:
        row = await db.get(OAuthToken, lookup.token_id)
        if row is None or row.kind != "access" or row.revoked_at is not None or row.expires_at <= now:
            raise invalid_token()
        if row.resource != resource:
            raise invalid_token("wrong resource")
        inst = await db.get(AppInstallation, row.installation_id)
        if inst is None or inst.status not in USABLE_INSTALLATION_STATUSES:
            raise invalid_token("installation inactive")
        state = await ecosystem_service.is_suspended(lookup.tenant_id, app_id=inst.app_id)
        if state.suspended and state.scope == "platform_app":
            raise invalid_token("app suspended")
        if state.suspended:
            raise ecosystem_suspended()
        team_ids: frozenset[uuid.UUID] = frozenset()
        bu_id = None
        if row.user_id is None:
            effective = app_token_scopes(list(row.scopes), list(inst.granted_scopes or []), None)
            permissions = dict.fromkeys(permissions_for(effective), Scope.TENANT)
        else:
            auth = (
                await db.get(AppUserAuthorization, row.user_authorization_id)
                if row.user_authorization_id
                else None
            )
            if row.user_authorization_id is not None and (auth is None or auth.revoked_at is not None):
                raise invalid_token("authorization revoked")
            user = await entity_refs.principal_for_user(db, lookup.tenant_id, row.user_id)
            if user is None:
                raise invalid_token("user inactive")
            effective = app_token_scopes(
                list(row.scopes),
                list(inst.granted_scopes or []),
                list(auth.scopes) if auth is not None else list(row.scopes),
            )
            permissions = delegated_permissions(user.permissions, effective)
            team_ids, bu_id = user.team_ids, user.business_unit_id
        if row.last_used_at is None or now - row.last_used_at >= LAST_USED_RESOLUTION:
            await db.execute(update(OAuthToken).where(OAuthToken.id == row.id).values(last_used_at=now))
        return Principal(
            type="app",
            id=inst.id,
            tenant_id=lookup.tenant_id,
            permissions=permissions,
            team_ids=team_ids,
            business_unit_id=bu_id,
            app_id=inst.app_id,
            app_installation_id=inst.id,
            oauth_token_id=row.id,
            acting_user_id=row.user_id,
            oauth_scopes=effective,
            resource=resource,
        )


async def authenticate_bearer(request: Request, token: str, *, resource: Literal["api", "mcp"]) -> Principal:  # noqa: ARG001
    token = token.strip()
    if not token.startswith(ACCESS_PREFIX):
        raise invalid_token()
    digest = hash_token(token)
    hit = _cache.get(digest)
    now = time.monotonic()
    if hit is not None and hit[1] > now:
        principal = hit[0]
        if principal.resource != resource:
            raise invalid_token("wrong resource")
        # The kill switch is re-checked on every call (its own 15 s cache), not frozen in the principal cache.
        state = await ecosystem_service.is_suspended(principal.tenant_id, app_id=principal.app_id)  # type: ignore[arg-type]
        if state.suspended:
            raise invalid_token("app suspended") if state.scope == "platform_app" else ecosystem_suspended()
    else:
        principal = await _resolve(token, resource)
        _cache[digest] = (principal, now + CACHE_SECONDS)
    update_context(principal=principal, tenant_id=principal.tenant_id)
    return principal


__all__ = [
    "app_token_scopes",
    "authenticate_bearer",
    "clear_cache",
    "delegated_permissions",
    "ecosystem_suspended",
    "invalid_token",
]
