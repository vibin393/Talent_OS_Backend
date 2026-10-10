# ruff: noqa: E501
"""Public API / MCP request log (Epic 66, NFR-74): ``register_request_log`` adds an after-response hook
(``scope["tos_after_response"]``, run by ``AfterResponseMiddleware``, as ``api_keys_service._register_request_log``)
that writes one ``api_request_log`` row and meters ``api_request`` (``error`` when status >= 400).

A missing monthly partition (``partitions.ensure_monthly`` not run) only loses the log row (logged); the request,
and the meter, are unaffected.
"""

import datetime as dt
import time
import uuid
from typing import Any

from loguru import logger

from app.core.context import RequestContext, get_context
from app.core.database import tenant_session
from app.models.metering import ApiRequestLog
from app.services.metering.record import record

API_VERSION = "v1"
HOOK_NAME = "_tos_public_request_log"


def route_group_of(template: str) -> str:
    """``/public/v1/candidates/{id}`` -> ``candidates`` (first path segment after the version)."""
    parts = [p for p in template.split("/") if p and not p.startswith("{")]
    for i, part in enumerate(parts):
        if part.startswith("v") and part[1:].isdigit():
            return parts[i + 1][:60] if i + 1 < len(parts) else ""
    return parts[-1][:60] if parts else ""


def register_request_log(request: Any, principal: Any) -> None:
    """Register the after-response writer once per request (idempotent)."""
    tenant_id = getattr(principal, "tenant_id", None)
    installation_id = getattr(principal, "app_installation_id", None)
    api_key_id = getattr(principal, "api_key_id", None)
    if getattr(principal, "type", None) == "api_key" and api_key_id is None:
        api_key_id = getattr(principal, "id", None)
    if tenant_id is None or (installation_id is None and api_key_id is None):
        return
    hooks = request.scope.setdefault("tos_after_response", [])
    if any(getattr(h, "__name__", "") == HOOK_NAME for h in hooks):
        return
    started = time.perf_counter()
    ctx = get_context()
    app_id = getattr(principal, "app_id", None)
    token_id = getattr(principal, "oauth_token_id", None)
    user_id = getattr(principal, "acting_user_id", None)

    async def _hook(status: int) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        route = request.scope.get("route")
        template = str(getattr(route, "path", None) or request.url.path)[:300]
        state = getattr(request, "state", None)
        group = str(getattr(state, "tos_route_group", "") or route_group_of(template))[:60]
        deprecated = bool(getattr(state, "tos_deprecated", False))
        try:
            async with tenant_session(tenant_id) as db:
                db.add(
                    ApiRequestLog(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        at=dt.datetime.now(dt.UTC),
                        installation_id=installation_id,
                        api_key_id=api_key_id,
                        token_id=token_id,
                        user_id=user_id,
                        method=str(request.method).upper()[:10],
                        route_template=template,
                        status=int(status),
                        latency_ms=latency_ms,
                        api_version=API_VERSION,
                        deprecated=deprecated,
                        request_id=(ctx.request_id or None) and str(ctx.request_id)[:128],
                        ip=(ctx.ip or None) and str(ctx.ip)[:64],
                    )
                )
        except Exception:
            logger.exception("Could not write the Public API request log (missing partition?)")
        await record(
            RequestContext(tenant_id=tenant_id),
            "api_request",
            1,
            installation_id,
            api_key_id=api_key_id,
            route_group=group,
            error=int(status) >= 400,
            app_id=app_id,
        )

    _hook.__name__ = HOOK_NAME
    hooks.append(_hook)


__all__ = ["API_VERSION", "register_request_log", "route_group_of"]
