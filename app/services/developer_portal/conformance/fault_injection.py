# ruff: file-ignore[line-too-long]
"""The fault resolver behind ``core.middleware.sandbox_faults`` (Epic 74, spec 0.6). Run state lives in the short-lived
store under ``sbx:conf:<installation>`` (+ an index ``sbx:conf:index`` of tenant -> installations with a running run,
so production traffic costs one cache read and no DB query).

Guards (both, every request): the bearer token's tenant is a Sandbox Tenant (``is_sandbox``) **and** a run is
``running`` for the token's Installation. Faults: the first ``POST`` -> 503 (``Retry-After: 2``, its
``Idempotency-Key`` recorded); the first ``GET`` after ``FAULT_429_AFTER_SECONDS`` -> 429 (``Retry-After: 5``).
"""

import time
import uuid
from typing import Any

from app.core.database import platform_session, tenant_session
from app.core.middleware.sandbox_faults import FaultDecision, InjectedResponse
from app.core.tenancy import is_sandbox
from app.models.oauth import OAuthToken, OAuthTokenLookup
from app.services.developer_portal.common import kv_delete, kv_get, kv_set
from app.services.developer_portal.conformance.checks import scopes_for

INDEX = "sbx:conf:index"
PREFIX = "sbx:conf:"
STATE_TTL = 6 * 3600
FAULT_429_AFTER_SECONDS = 120.0
MAX_CALLS = 2000


def clock() -> float:
    return time.time()


def state_key(installation_id: uuid.UUID | str) -> str:
    return f"{PREFIX}{installation_id}"


async def activate(run_id: uuid.UUID, tenant_id: uuid.UUID, installation_id: uuid.UUID) -> None:
    await kv_set(state_key(installation_id), {"run_id": str(run_id), "tenant_id": str(tenant_id), "installation_id": str(installation_id),
                                              "started": clock(), "calls": [], "inj503": None, "inj429": None, "uninstalled_at": None, "webhook": {}}, STATE_TTL)  # fmt: skip
    index = await kv_get(INDEX) or {}
    index.setdefault(str(tenant_id), [])
    if str(installation_id) not in index[str(tenant_id)]:
        index[str(tenant_id)].append(str(installation_id))
    await kv_set(INDEX, index, STATE_TTL)


async def deactivate(tenant_id: uuid.UUID, installation_id: uuid.UUID) -> dict[str, Any] | None:
    state = await kv_get(state_key(installation_id))
    await kv_delete(state_key(installation_id))
    index = await kv_get(INDEX) or {}
    left = [i for i in index.get(str(tenant_id), []) if i != str(installation_id)]
    if left:
        index[str(tenant_id)] = left
    else:
        index.pop(str(tenant_id), None)
    await kv_set(INDEX, index, STATE_TTL)
    return state


async def get_state(installation_id: uuid.UUID) -> dict[str, Any] | None:
    return await kv_get(state_key(installation_id))


async def update_state(installation_id: uuid.UUID, **values: Any) -> None:
    state = await kv_get(state_key(installation_id))
    if state is not None:
        state.update(values)
        await kv_set(state_key(installation_id), state, STATE_TTL)


TOKEN_PREFIX = "sbx:conf:tok:"


async def _token(headers: dict[str, str], index: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID, str] | None:
    """(tenant, installation, grant) of the bearer token; remembered per token hash while a run is active, so calls
    made after uninstall (when 67 deletes the lookups) are still attributed."""
    from app.services import oauth_service

    auth = headers.get("authorization") or ""
    if auth[:7].lower() != "bearer ":
        return None
    digest = oauth_service.hash_token(auth[7:].strip())
    cached = await kv_get(TOKEN_PREFIX + digest.hex())
    if cached is not None:
        return uuid.UUID(cached["tenant_id"]), uuid.UUID(cached["installation_id"]), str(cached["grant"])
    async with platform_session() as db:
        lookup = await db.get(OAuthTokenLookup, digest)
    if lookup is None or lookup.kind != "access" or str(lookup.tenant_id) not in index:
        return None
    async with tenant_session(lookup.tenant_id) as db:
        tok = await db.get(OAuthToken, lookup.token_id)
    if tok is None or str(tok.installation_id) not in index[str(lookup.tenant_id)]:
        return None
    await kv_set(TOKEN_PREFIX + digest.hex(), {"tenant_id": str(lookup.tenant_id), "installation_id": str(tok.installation_id), "grant": tok.grant_type}, STATE_TTL)
    return lookup.tenant_id, tok.installation_id, tok.grant_type


async def resolve(method: str, path: str, headers: dict[str, str]) -> FaultDecision | None:
    index = await kv_get(INDEX)
    if not index:
        return None
    found = await _token(headers, index)
    if found is None:
        return None
    tenant_id, installation_id, grant = found
    if str(installation_id) not in index.get(str(tenant_id), []) or not await is_sandbox(tenant_id):
        return None
    state = await get_state(installation_id)
    if state is None or state.get("tenant_id") != str(tenant_id):
        return None
    now = clock()
    idem = headers.get("idempotency-key")
    inject: InjectedResponse | None = None
    tag: str | None = None
    if state.get("uninstalled_at") is None:
        if method == "POST" and state.get("inj503") is None:
            inject = InjectedResponse(503, "service_unavailable", "Temporarily unavailable (conformance run)", 2)
            state["inj503"] = {"t": now, "key": idem}
            tag = "503"
        elif method == "GET" and state.get("inj429") is None and now - float(state["started"]) >= FAULT_429_AFTER_SECONDS:
            inject = InjectedResponse(429, "rate_limited", "Too many requests (conformance run)", 5)
            state["inj429"] = {"t": now}
            tag = "429"
    await kv_set(state_key(installation_id), state, STATE_TTL)
    route_scopes = scopes_for(method, path)

    async def observe(status: int) -> None:
        fresh = await get_state(installation_id)
        if fresh is None:
            return
        calls = list(fresh.get("calls") or [])
        if len(calls) < MAX_CALLS:
            calls.append({"t": now, "m": method, "p": path[:200], "s": status, "g": grant, "k": idem, "sc": route_scopes, "i": tag})
        fresh["calls"] = calls
        if tag == "503" and fresh.get("inj503") is None:
            fresh["inj503"] = {"t": now, "key": idem}
        if tag == "429" and fresh.get("inj429") is None:
            fresh["inj429"] = {"t": now}
        await kv_set(state_key(installation_id), fresh, STATE_TTL)

    return FaultDecision(inject=inject, observe=observe)


__all__ = ["FAULT_429_AFTER_SECONDS", "activate", "clock", "deactivate", "get_state", "resolve", "state_key", "update_state"]
