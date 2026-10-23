# ruff: file-ignore[line-too-long, non-imperative-mood]
"""OAuth token store: minting and revocation (Epic 66, AD-76, spec 0.6).

Opaque tokens (``tos_at_`` access 60 min, ``tos_rt_`` refresh 30 d idle / 90 d absolute per family, ``tos_ac_`` codes,
``tos_cs_`` client secrets), >= 256 bits; only ``sha256`` is stored (``oauth_tokens``) plus one platform lookup row per
token (``oauth_token_lookups``). ``issue_token`` enforces: the Installation is active and its App not suspended; scopes are
a subset of the Grant; D-7 (no sensitive scope without a user); resource ``mcp`` needs a user. ``revoke_for`` sets
``revoked_at``, deletes the lookups and emits ``OAuthTokenFamilyRevoked`` per family.
"""

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.database import platform_session
from app.core.errors import ConflictError, ValidationFailed
from app.core.events import emit
from app.core.oauth_scopes import get_scope, scopes
from app.core.settings import get_settings
from app.events.ecosystem import OAuthTokenFamilyRevoked
from app.models.oauth import App, AppInstallation, OAuthToken, OAuthTokenLookup
from app.schemas.oauth import GrantType, IssuedTokens

ACCESS_PREFIX = "tos_at_"
REFRESH_PREFIX = "tos_rt_"
CODE_PREFIX = "tos_ac_"
SECRET_PREFIX = "tos_cs_"
USABLE_INSTALLATION_STATUSES = frozenset({"active", "degraded"})
REVOKE_REASONS = frozenset(
    {
        "uninstalled",
        "suspended",
        "kill_switch",
        "user_deactivated",
        "grant_reduced",
        "reuse_detected",
        "revoked",
        "rotated_secret",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.strip().encode("utf-8")).digest()


def new_token(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


def _invalid_scope(message: str, scope: str | None = None) -> ValidationFailed:
    return ValidationFailed(message, code="invalid_scope", details={"scope": scope} if scope else {})


def check_scopes(
    requested: Sequence[str], *, granted: Sequence[str], user_id: uuid.UUID | None, resource: str
) -> list[str]:
    """The validated scope list (sorted, unique) or 400 ``invalid_scope``. Pure apart from the scope registry."""
    catalogue = scopes()
    out = sorted(set(requested))
    if not out:
        raise _invalid_scope("At least one scope is required")
    granted_set = set(granted)
    for key in out:
        if key not in catalogue:
            raise _invalid_scope(f"Unknown scope {key}", key)
        if key not in granted_set:
            raise _invalid_scope(f"Scope {key} is not granted to this installation", key)
        spec = get_scope(key)
        if (
            spec.sensitive and user_id is None
        ):  # D-7: sensitive scopes are delegated-only, for every publisher kind
            raise _invalid_scope(f"Scope {key} needs a user's authorization", key)
        if key.startswith("mcp:") and resource != "mcp":
            raise _invalid_scope(f"Scope {key} is only valid for the MCP resource", key)
        # D-25(a): MCP tokens may carry granted base scopes alongside mcp:* (unredaction when the user holds them).
        if resource not in spec.resources and not (resource == "mcp" and "api" in spec.resources):
            raise _invalid_scope(f"Scope {key} is not valid for resource {resource}", key)
    if resource == "mcp" and not any(k.startswith("mcp:") for k in out):
        raise _invalid_scope("An MCP token needs an mcp:* scope")
    return out


async def issue_token(
    db: AsyncSession,
    *,
    installation_id: uuid.UUID,
    grant_type: GrantType,
    resource: Literal["api", "mcp"],
    scopes: Sequence[str],
    user_id: uuid.UUID | None = None,
    user_authorization_id: uuid.UUID | None = None,
    family_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    with_refresh: bool = False,
    issued_from_code_id: uuid.UUID | None = None,
) -> IssuedTokens:
    """Mint an access token (+ a refresh token when ``with_refresh`` and delegated). ``db`` is the tenant session."""
    s = get_settings()
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    inst = await db.get(AppInstallation, installation_id)
    if inst is None or inst.kind != "app" or inst.status not in USABLE_INSTALLATION_STATUSES:
        raise ConflictError("The app is not installed or not active", code="app_not_installed")
    async with platform_session() as pdb:
        app = await pdb.get(App, inst.app_id)
        app_status = app.status if app is not None else None
    if app_status in (None, "suspended", "retired"):
        raise ConflictError("This app is unavailable", code="app_unavailable")
    if resource == "mcp" and user_id is None:
        raise _invalid_scope("MCP tokens are delegated-only (authorization code flow)")
    if grant_type == "client_credentials" and (user_id is not None or resource != "api"):
        raise _invalid_scope("Client credentials tokens are app-only and for the API resource")
    if grant_type != "client_credentials" and user_id is None:
        raise _invalid_scope("A user is required for this grant type")
    granted = list(inst.granted_scopes or [])
    checked = check_scopes(scopes, granted=granted, user_id=user_id, resource=resource)
    now = _now()
    family = family_id or uuid.uuid4()
    access = new_token(ACCESS_PREFIX)
    access_row = OAuthToken(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        installation_id=installation_id,
        kind="access",
        token_hash=hash_token(access),
        prefix=access[:12],
        grant_type=grant_type,
        resource=resource,
        user_id=user_id,
        user_authorization_id=user_authorization_id,
        scopes=checked,
        family_id=family,
        parent_id=parent_id,
        issued_from_code_id=issued_from_code_id,
        expires_at=now + timedelta(seconds=s.oauth_access_ttl_seconds),
    )
    rows = [access_row]
    refresh: str | None = None
    if with_refresh and user_id is not None:
        refresh = new_token(REFRESH_PREFIX)
        absolute = now + timedelta(days=s.oauth_refresh_absolute_days)
        if family_id is not None:  # rotation keeps the family's absolute cap
            first = (
                await db.execute(
                    select(OAuthToken.absolute_expires_at)
                    .where(OAuthToken.family_id == family_id, OAuthToken.kind == "refresh")
                    .order_by(OAuthToken.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            absolute = first or absolute
        rows.append(
            OAuthToken(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                installation_id=installation_id,
                kind="refresh",
                token_hash=hash_token(refresh),
                prefix=refresh[:12],
                grant_type=grant_type,
                resource=resource,
                user_id=user_id,
                user_authorization_id=user_authorization_id,
                scopes=checked,
                family_id=family,
                parent_id=parent_id,
                issued_from_code_id=issued_from_code_id,
                expires_at=min(now + timedelta(days=s.oauth_refresh_idle_days), absolute),
                absolute_expires_at=absolute,
            )
        )
    for row in rows:
        db.add(row)
    await db.flush()
    async with platform_session() as pdb:
        for row in rows:
            pdb.add(
                OAuthTokenLookup(
                    token_hash=row.token_hash,
                    tenant_id=tenant_id,
                    token_id=row.id,
                    kind=row.kind,
                    expires_at=row.expires_at,
                )
            )
    audit.record(
        "oauth.token_issued", "app_installation", installation_id, session=db, grant_type=grant_type,
        resource=resource, scopes=checked, user_id=user_id, family_id=family, with_refresh=refresh is not None,
    )  # fmt: skip
    return IssuedTokens(
        access_token=access,
        refresh_token=refresh,
        expires_in=s.oauth_access_ttl_seconds,
        scopes=checked,
        token_id=access_row.id,
        family_id=family,
    )


async def revoke_for(
    db: AsyncSession,
    *,
    installation_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    app_id: uuid.UUID | None = None,
    family_id: uuid.UUID | None = None,
    reason: str,
) -> int:
    """Revoke every live token matching all given filters (tenant session); returns the count."""
    if reason not in REVOKE_REASONS:
        msg = f"unknown revoke reason {reason!r}"
        raise ValueError(msg)
    if installation_id is None and user_id is None and app_id is None and family_id is None:
        msg = "revoke_for needs at least one filter"
        raise ValueError(msg)
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    stmt = select(OAuthToken).where(OAuthToken.revoked_at.is_(None))
    if installation_id is not None:
        stmt = stmt.where(OAuthToken.installation_id == installation_id)
    if user_id is not None:
        stmt = stmt.where(OAuthToken.user_id == user_id)
    if family_id is not None:
        stmt = stmt.where(OAuthToken.family_id == family_id)
    if app_id is not None:
        stmt = stmt.where(
            OAuthToken.installation_id.in_(select(AppInstallation.id).where(AppInstallation.app_id == app_id))
        )
    rows = list((await db.execute(stmt.with_for_update())).scalars())
    if not rows:
        return 0
    now = _now()
    ids = [r.id for r in rows]
    await db.execute(
        update(OAuthToken).where(OAuthToken.id.in_(ids)).values(revoked_at=now, revoked_reason=reason)
    )
    hashes = [r.token_hash for r in rows]
    async with platform_session() as pdb:
        await pdb.execute(delete(OAuthTokenLookup).where(OAuthTokenLookup.token_hash.in_(hashes)))
    families: dict[uuid.UUID, OAuthToken] = {}
    for r in rows:
        families.setdefault(r.family_id, r)
    for fam, r in families.items():
        emit(
            db,
            OAuthTokenFamilyRevoked(
                tenant_id=tenant_id,
                installation_id=r.installation_id,
                family_id=fam,
                user_id=r.user_id,
                reason=reason,
            ),
        )
    audit.record(
        "oauth.tokens_revoked", "app_installation", installation_id, session=db, reason=reason, count=len(rows),
        families=len(families), user_id=user_id, app_id=app_id,
    )  # fmt: skip
    from app.services.oauth.principals import clear_cache

    clear_cache()
    return len(rows)


async def purge_lookups(now: datetime | None = None) -> int:
    """Hourly (``maintenance``): delete expired platform lookup rows."""
    async with platform_session() as pdb:
        result = await pdb.execute(
            delete(OAuthTokenLookup).where(OAuthTokenLookup.expires_at < (now or _now()))
        )
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "ACCESS_PREFIX",
    "CODE_PREFIX",
    "REFRESH_PREFIX",
    "SECRET_PREFIX",
    "check_scopes",
    "hash_token",
    "issue_token",
    "new_token",
    "purge_lookups",
    "revoke_for",
]
