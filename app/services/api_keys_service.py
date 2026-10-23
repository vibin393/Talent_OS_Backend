"""Tenant API keys (Epic 12 story 12.7, FR-75).

* Key format ``tos_<prefix>_<token_urlsafe(32)>``; only ``sha256(key)`` is stored (``api_keys.key_hash`` and
  the platform lookup ``api_key_lookups``). The full key is returned once, at creation/rotation.
* Scopes are a subset of the creator's own permissions (422 ``scope_not_held``) and never one of
  ``NON_DELEGABLE`` (422 ``scope_not_delegable``); every scope is evaluated at tenant scope.
* ``authenticate`` resolves ``Authorization: Bearer tos_...``: lookup -> ``tenant_session`` -> not revoked, not
  expired, tenant active -> ``Principal(type="api_key")``. Failures are 401 ``invalid_api_key`` (audited,
  sampled to one entry per prefix per minute); a suspended tenant is 403 ``tenant_suspended``. Per-key rate
  limit ``rate_limit_per_min``; every request writes an ``api_key_request_log`` row; ``last_used_*`` is written
  at most once a minute.
"""

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, rate_limit
from app.core.context import RequestContext, get_context, update_context
from app.core.database import platform_session, tenant_session
from app.core.errors import Forbidden, NotFound, Unauthorized, ValidationFailed
from app.core.permissions import CATALOG, NON_DELEGABLE, Principal, Scope
from app.models.api_keys import ApiKey, ApiKeyLookup, ApiKeyRequestLog
from app.schemas.api_keys import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyUsageOut, ApiKeyUsageRow
from app.services import identity_bridge

KEY_RE = re.compile(r"^tos_([0-9a-f]{8})_([A-Za-z0-9_-]{40,64})$")
LAST_USED_RESOLUTION = timedelta(minutes=1)


def _now() -> datetime:
    return datetime.now(UTC)


def hash_key(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


def public_scopes_for(scopes: list[str]) -> list[str]:
    """Epic 67 (FR-348): the Public API scopes a key holding these permissions unlocks (``api_key_allowed`` scopes whose
    every mapped permission the key holds; gate-only scopes with no permission are not unlocked)."""
    from app.core.oauth_scopes import scopes as scope_catalogue

    held = set(scopes)
    return sorted(
        key
        for key, spec in scope_catalogue().items()
        if spec.api_key_allowed and spec.permissions and "api" in spec.resources and set(spec.permissions) <= held
    )


def delegable_scopes(permissions: dict[str, Any]) -> list[str]:
    return sorted(p for p in permissions if p not in NON_DELEGABLE and p in CATALOG)


def _status(row: ApiKey, now: datetime | None = None) -> str:
    now = now or _now()
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at is not None and row.expires_at <= now:
        return "expired"
    return "active"


def _out(row: ApiKey) -> ApiKeyOut:
    out = ApiKeyOut.model_validate(row)
    out.status = _status(row)
    return out


def _validate_scopes(scopes: list[str], held: dict[str, Any]) -> list[str]:
    fields: dict[str, str] = {}
    code = "validation_failed"
    for scope in scopes:
        if scope not in CATALOG:
            fields[scope] = "Unknown permission"
        elif scope in NON_DELEGABLE:
            fields[scope] = "This permission can never be given to an API key"
            code = "scope_not_delegable"
        elif scope not in held:
            fields[scope] = "You do not hold this permission"
            code = "scope_not_held" if code == "validation_failed" else code
    if fields:
        raise ValidationFailed("Invalid API key scopes", code=code, details={"fields": fields})
    return sorted(set(scopes))


async def _insert(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str,
    scopes: list[str],
    rate_limit_per_min: int,
    expires_at: datetime | None,
    created_by: uuid.UUID | None,
    rotated_from_id: uuid.UUID | None = None,
) -> tuple[ApiKey, str]:
    prefix = secrets.token_hex(4)
    key = f"tos_{prefix}_{secrets.token_urlsafe(32)}"
    digest = hash_key(key)
    row = ApiKey(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        prefix=prefix,
        key_hash=digest,
        scopes=scopes,
        rate_limit_per_min=rate_limit_per_min,
        expires_at=expires_at,
        created_by=created_by,
        rotated_from_id=rotated_from_id,
    )
    db.add(row)
    db.add(ApiKeyLookup(key_hash=digest, tenant_id=tenant_id, key_id=row.id))
    await db.flush()
    await db.refresh(row)
    return row, key


async def create_key(ctx: RequestContext, db: AsyncSession, data: ApiKeyCreate) -> ApiKeyCreated:
    principal = ctx.principal
    assert ctx.tenant_id is not None and principal is not None
    scopes = _validate_scopes(data.scopes, dict(principal.permissions))
    if data.expires_at is not None and data.expires_at <= _now():
        raise ValidationFailed(
            "Expiry must be in the future", details={"fields": {"expires_at": "Must be in the future"}}
        )
    row, key = await _insert(
        db,
        ctx.tenant_id,
        name=data.name,
        scopes=scopes,
        rate_limit_per_min=data.rate_limit_per_min,
        expires_at=data.expires_at,
        created_by=principal.id,
    )
    return ApiKeyCreated(**_out(row).model_dump(), key=key)


async def list_keys(ctx: RequestContext, db: AsyncSession) -> list[ApiKeyOut]:  # ruff: ignore[unused-function-argument]
    rows = (await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars()
    return [_out(r) for r in rows]


async def _get(db: AsyncSession, key_id: uuid.UUID) -> ApiKey:
    row = await db.get(ApiKey, key_id, with_for_update=True)
    if row is None:
        raise NotFound("API key not found")
    return row


async def rotate_key(
    ctx: RequestContext, db: AsyncSession, key_id: uuid.UUID, grace_hours: int
) -> ApiKeyCreated:
    principal = ctx.principal
    assert ctx.tenant_id is not None and principal is not None
    old = await _get(db, key_id)
    if _status(old) != "active":
        from app.core.errors import ConflictError

        raise ConflictError("Only an active key can be rotated", code="api_key_inactive")
    row, key = await _insert(
        db,
        ctx.tenant_id,
        name=old.name,
        scopes=list(old.scopes),
        rate_limit_per_min=old.rate_limit_per_min,
        expires_at=old.expires_at,
        created_by=principal.id,
        rotated_from_id=old.id,
    )
    grace_end = _now() + timedelta(hours=grace_hours)
    old.expires_at = grace_end if old.expires_at is None else min(old.expires_at, grace_end)
    await db.flush()
    audit.record("api_key.rotated", "api_key", old.id, session=db, new_key_id=row.id, grace_hours=grace_hours)
    return ApiKeyCreated(**_out(row).model_dump(), key=key)


async def revoke_key(ctx: RequestContext, db: AsyncSession, key_id: uuid.UUID) -> ApiKeyOut:
    row = await _get(db, key_id)
    if row.revoked_at is None:
        row.revoked_at = _now()
        row.revoked_by = getattr(ctx.principal, "id", None)
        await db.flush()
    return _out(row)


async def usage(ctx: RequestContext, db: AsyncSession, key_id: uuid.UUID) -> ApiKeyUsageOut:  # ruff: ignore[unused-function-argument]
    if await db.get(ApiKey, key_id) is None:
        raise NotFound("API key not found")
    since = _now() - timedelta(hours=24)
    by_status = {
        str(status): int(n)
        for status, n in (
            await db.execute(
                select(ApiKeyRequestLog.status, func.count())
                .where(ApiKeyRequestLog.key_id == key_id, ApiKeyRequestLog.at >= since)
                .group_by(ApiKeyRequestLog.status)
            )
        ).all()
    }
    recent = (
        await db.execute(
            select(ApiKeyRequestLog)
            .where(ApiKeyRequestLog.key_id == key_id)
            .order_by(ApiKeyRequestLog.at.desc())
            .limit(100)
        )
    ).scalars()
    return ApiKeyUsageOut(
        total_last_24h=sum(by_status.values()),
        by_status=by_status,
        recent=[ApiKeyUsageRow.model_validate(r) for r in recent],
    )


# ================================================================ authentication
def _invalid() -> Unauthorized:
    return Unauthorized("Invalid or expired API key", code="invalid_api_key")


async def _audit_failure(prefix: str, reason: str, tenant_id: uuid.UUID | None = None) -> None:
    """At most one audit entry per key prefix per minute (in-process sample)."""
    sample = await rate_limit.memory_backend().incr(
        f"apikey-audit:{prefix}", f"apikey-audit-prev:{prefix}", 1, 60_000, 0.0
    )
    if not sample[0]:
        return
    try:
        cm = tenant_session(tenant_id) if tenant_id is not None else platform_session()
        async with cm as db:
            audit.record(
                "api_key.auth_failed",
                "api_key",
                None,
                session=db,
                actor_type="api_key",
                actor_id=None,
                prefix=prefix,
                reason=reason,
            )
    except Exception:
        logger.exception("Could not audit an API key failure")


async def authenticate(request: Request, key: str) -> Principal:
    match = KEY_RE.fullmatch(key.strip())
    prefix = match.group(1) if match else "invalid"
    if match is None:
        await _audit_failure(prefix, "malformed")
        raise _invalid()
    digest = hash_key(key.strip())
    async with platform_session() as pdb:
        lookup = await pdb.get(ApiKeyLookup, digest)
        tenant_status = (
            await identity_bridge.tenant_status(pdb, lookup.tenant_id) if lookup is not None else None
        )
    if lookup is None:
        await _audit_failure(prefix, "unknown")
        raise _invalid()
    now = _now()
    async with tenant_session(lookup.tenant_id) as db:
        row = await db.get(ApiKey, lookup.key_id)
        reason = None
        if row is None:
            reason = "unknown"
        elif row.revoked_at is not None:
            reason = "revoked"
        elif row.expires_at is not None and row.expires_at <= now:
            reason = "expired"
        if (
            reason is None
            and row is not None
            and (row.last_used_at is None or now - row.last_used_at >= LAST_USED_RESOLUTION)
        ):
            row.last_used_at = now
            row.last_used_ip = get_context().ip
        key_id = row.id if row is not None else None
        scopes = list(row.scopes) if row is not None else []
        limit = row.rate_limit_per_min if row is not None else 60
    if reason is not None:
        await _audit_failure(prefix, reason, lookup.tenant_id)
        raise _invalid()
    if tenant_status != "active":
        raise Forbidden("This workspace is suspended", code="tenant_suspended")
    assert key_id is not None
    principal = Principal(
        type="api_key",
        id=key_id,
        tenant_id=lookup.tenant_id,
        permissions={s: Scope.TENANT for s in scopes if s in CATALOG},
        api_key_id=key_id,
    )
    update_context(principal=principal, tenant_id=lookup.tenant_id)
    _register_request_log(request, lookup.tenant_id, key_id)
    await rate_limit.enforce("api_key", str(key_id), limit, 60)
    return principal


def _register_request_log(request: Request, tenant_id: uuid.UUID, key_id: uuid.UUID) -> None:
    """Write one ``api_key_request_log`` row once the response status is known (request-hooks middleware)."""
    hooks = request.scope.setdefault("tos_after_response", [])
    ctx = get_context()

    async def _write(status: int) -> None:
        route = request.scope.get("route")
        try:
            async with tenant_session(tenant_id) as db:
                db.add(
                    ApiKeyRequestLog(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        key_id=key_id,
                        at=_now(),
                        method=request.method.upper(),
                        route_template=str(getattr(route, "path", request.url.path))[:300],
                        status=status,
                        ip=ctx.ip,
                        request_id=ctx.request_id,
                    )
                )
        except Exception:
            logger.exception("Could not write the API key request log")

    if not any(getattr(h, "__name__", "") == "_write" for h in hooks):
        hooks.append(_write)
