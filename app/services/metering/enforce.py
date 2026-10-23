# ruff: file-ignore[line-too-long]
"""Rate Tier enforcement for Public API / MCP callers (Epic 66, AD-78, FR-338).

* App principals (``type="app"``): burst ``tier:sec:{tenant}:{installation}`` (1 s), ``tier:min:{tenant}:{installation}``
  (60 s), delegated tokens also ``tier:user:{tenant}:{user}`` (60 s, ``delegated_user_per_min``) -> 429
  ``rate_limited`` with ``Retry-After`` (``core.rate_limit``).
* API keys keep their own ``rate_limit_per_min`` (``api_keys_service``); here they get only the day quota.
* Day quota: one counter per tenant and UTC day (``tier:day:{tenant}:{yyyymmdd}``) against the caller's tier
  ``per_day`` -> 429 ``quota_exceeded`` with ``Retry-After`` = seconds until 00:00 UTC.

Tier resolution (``core.rate_tiers.resolve_tier_name``) reads ``app_installations.rate_tier``, ``apps.rate_tier``,
``ecosystem_settings.rate_tier``, ``tenants.rate_tier`` and ``tenants.mode`` once per (tenant, installation) per 60 s.
No-op while ``RATE_LIMIT_ENABLED`` is false (as every ``core.rate_limit`` policy).
"""

import math
import time
import uuid
from typing import Any

from sqlalchemy import text

from app.core import rate_limit
from app.core.database import tenant_session
from app.core.errors import RateLimited
from app.core.rate_tiers import RateTier, resolve_tier_name, tier
from app.core.settings import get_settings

TIER_CACHE_SECONDS = 60.0
DAY = 86_400
_cache: dict[tuple[str, str], tuple[str, float]] = {}

_TIER_SQL = text(
    "SELECT t.rate_tier AS tenant_tier, t.mode AS mode, "
    "(SELECT es.rate_tier FROM ecosystem_settings es WHERE es.tenant_id = t.id LIMIT 1) AS ecosystem_tier, "
    "(SELECT ai.rate_tier FROM app_installations ai WHERE ai.id = CAST(:inst AS uuid)) AS installation_tier, "
    "(SELECT a.rate_tier FROM app_installations ai JOIN apps a ON a.id = ai.app_id "
    " WHERE ai.id = CAST(:inst AS uuid)) AS app_tier "
    "FROM tenants t WHERE t.id = CAST(:tid AS uuid)"
)


def clear_tier_cache(tenant_id: uuid.UUID | str | None = None) -> None:
    """Forget cached tiers (all, or one tenant's)."""
    if tenant_id is None:
        _cache.clear()
        return
    for key in [k for k in _cache if k[0] == str(tenant_id)]:
        _cache.pop(key, None)


async def resolve_tier(tenant_id: uuid.UUID, installation_id: uuid.UUID | None = None) -> RateTier:
    key = (str(tenant_id), str(installation_id or ""))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[1] > now:
        return tier(hit[0])
    async with tenant_session(tenant_id) as db:
        row = (
            (await db.execute(_TIER_SQL, {"tid": str(tenant_id), "inst": key[1] or None})).mappings().first()
        )
    if row is None:
        name = resolve_tier_name(
            installation_tier=None, app_tier=None, ecosystem_tier=None, tenant_tier=None, sandbox=False
        )
    else:
        name = resolve_tier_name(
            installation_tier=row["installation_tier"],
            app_tier=row["app_tier"],
            ecosystem_tier=row["ecosystem_tier"],
            tenant_tier=row["tenant_tier"],
            sandbox=row["mode"] == "sandbox",
        )
    _cache[key] = (name, now + TIER_CACHE_SECONDS)
    return tier(name)


def seconds_until_utc_midnight(now: float | None = None) -> int:
    now = time.time() if now is None else now
    return max(1, math.ceil((math.floor(now / DAY) + 1) * DAY - now))


def quota_exceeded(limit: int, retry_after: int) -> RateLimited:
    exc = RateLimited(
        "Daily request quota exceeded; it resets at 00:00 UTC",
        code="quota_exceeded",
        details={"retry_after": retry_after, "limit": limit},
    )
    headers = {
        "Retry-After": str(retry_after),
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": str(retry_after),
    }

    def _apply(response: object) -> None:
        for k, v in headers.items():
            response.headers[k] = v  # type: ignore[attr-defined]

    exc.response_hooks.append(_apply)
    return exc


async def _day_quota(tenant_id: uuid.UUID, limit: int) -> None:
    now = time.time()
    day = time.strftime("%Y%m%d", time.gmtime(now))
    # window = one day: the bucket (floor(now / 86400)) is constant within a UTC date, so a date-scoped subject never
    # has a previous bucket and the sliding window degenerates to a fixed daily counter.
    result = await rate_limit.check("tier:day", f"{tenant_id}:{day}", limit, DAY)
    if not result.allowed:
        raise quota_exceeded(limit, seconds_until_utc_midnight(now))


async def enforce(principal: Any, route_group: str = "") -> None:  # ruff: ignore[unused-function-argument] - route_group reserved (per-group limits)
    """Apply the caller's Rate Tier. Raises 429 ``rate_limited`` / ``quota_exceeded``."""
    if not get_settings().rate_limit_enabled:
        return
    tenant_id = getattr(principal, "tenant_id", None)
    if tenant_id is None:
        return
    installation_id = getattr(principal, "app_installation_id", None)
    api_key_id = getattr(principal, "api_key_id", None)
    if getattr(principal, "type", None) == "api_key" and api_key_id is None:
        api_key_id = getattr(principal, "id", None)
    t = await resolve_tier(tenant_id, installation_id)
    if installation_id is not None:
        subject = f"{tenant_id}:{installation_id}"
        await rate_limit.enforce("tier:sec", subject, t.burst_per_second, 1)
        await rate_limit.enforce("tier:min", subject, t.per_minute, 60)
        user_id = getattr(principal, "acting_user_id", None)
        if user_id is not None:
            await rate_limit.enforce("tier:user", f"{tenant_id}:{user_id}", t.delegated_user_per_min, 60)
    elif api_key_id is None:
        return
    await _day_quota(tenant_id, t.per_day)


__all__ = ["clear_tier_cache", "enforce", "quota_exceeded", "resolve_tier", "seconds_until_utc_midnight"]
