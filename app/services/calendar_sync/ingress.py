# ruff: noqa: E501
"""Push notification ingress (Epic 72 story 72.3, spec A4). Public endpoint ``/api/v1/hooks/calendar/{channel_id}``.

* ``?validationToken=`` (Graph handshake / lifecycle validation) -> 200 ``text/plain`` echo, nothing else.
* The tenant comes from the platform lookup (404 when unknown); the channel secret from the vault.
* Google: ``X-Goog-Channel-Token`` (constant time) and ``X-Goog-Resource-ID`` must match, else 401; ``sync`` -> 200.
* Microsoft: items with a wrong ``clientState`` / ``subscriptionId`` are dropped and counted; the answer is always
  202 (a forged request must not get a valid subscription disabled). Lifecycle events renew / fall back / resync.
* Notifications only trigger a delta sync (bodies are never trusted as data), at most one per channel per 30 s
  (coalesced; a notification during the window queues one follow-up). Ecosystem Kill Switch on: acknowledged only.
"""

import dataclasses
import hmac
import json
import uuid
from collections import Counter
from collections.abc import Mapping
from typing import Any

from loguru import logger

from app.core.context import reset_context, set_context
from app.core.database import tenant_session
from app.services.calendar_sync import channels
from app.services.calendar_sync.common import DEDUP_SECONDS, now, system_ctx

METRICS: Counter[str] = Counter()  # calendar_bad_client_state, calendar_bad_token, calendar_accepted, ...
MAX_VALIDATION_TOKEN = 1024


@dataclasses.dataclass(frozen=True, slots=True)
class IngressResult:
    status_code: int
    text: str | None = None
    queued: bool = False


def _eq(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a).encode(), str(b).encode())


async def _dedup(channel_id: uuid.UUID) -> bool:
    """Return True to enqueue now; False when a run is already queued in the window (a follow-up is recorded)."""
    from app.services.connections.store import get_store

    store = get_store()
    key = f"calendar:delta:{channel_id}"
    if await store.get(key) is not None:
        await store.put(f"{key}:followup", "1", DEDUP_SECONDS * 4)
        return False
    await store.put(key, "1", DEDUP_SECONDS)
    return True


def _enqueue(tenant_id: uuid.UUID, channel_id: uuid.UUID, *, full: bool = False) -> None:
    from app.tasks import calendar_sync as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.delta, str(tenant_id), str(channel_id), full)


async def handle(channel_id: uuid.UUID, headers: Mapping[str, str], query: Mapping[str, str], body: bytes) -> IngressResult:
    validation = query.get("validationToken")
    if validation is not None:
        return IngressResult(200, validation[:MAX_VALIDATION_TOKEN])
    tenant_id = await channels.tenant_for(channel_id)
    if tenant_id is None:
        return IngressResult(404)
    lower = {k.lower(): v for k, v in headers.items()}
    token = set_context(system_ctx(tenant_id))
    try:
        return await _handle_tenant(tenant_id, channel_id, lower, query, body)
    finally:
        reset_context(token)


async def _handle_tenant(
    tenant_id: uuid.UUID, channel_id: uuid.UUID, headers: Mapping[str, str], query: Mapping[str, str], body: bytes  # noqa: ARG001
) -> IngressResult:
    from app.services import ecosystem_service

    google = "x-goog-channel-id" in headers or "x-goog-channel-token" in headers
    full = False
    async with tenant_session(tenant_id) as db:
        ch = await channels.by_channel_id(db, channel_id)
        if ch is None or ch.status in ("stopped", "failed"):
            return IngressResult(404)
        secret = await channels.channel_secret(db, ch.connection_id)
        if google:
            if not (_eq(headers.get("x-goog-channel-token"), secret) and _eq(headers.get("x-goog-resource-id"), ch.provider_resource_id)):
                METRICS["calendar_bad_token"] += 1
                return IngressResult(401)
            if headers.get("x-goog-resource-state") == "sync":
                return IngressResult(200)
        else:
            try:
                payload: Any = json.loads(body or b"{}")
            except ValueError:
                METRICS["calendar_bad_client_state"] += 1
                return IngressResult(202)
            items = payload.get("value") if isinstance(payload, dict) else None
            valid = []
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and _eq(item.get("clientState"), secret) and _eq(item.get("subscriptionId"), ch.provider_resource_id):
                    valid.append(item)
                else:
                    METRICS["calendar_bad_client_state"] += 1
            if not valid:
                return IngressResult(202)
            for item in valid:
                event = item.get("lifecycleEvent")
                if event == "reauthorizationRequired":
                    ch.renew_after = now()
                elif event == "subscriptionRemoved":
                    ch.status = "polling"
                    ch.next_poll_at = now()
                    ch.renew_after = now()
                elif event == "missed":
                    full = True
        ch.last_notification_at = now()
        ch.sync_requested_at = now()
        await db.commit()
    if (await ecosystem_service.is_suspended(tenant_id)).suspended:
        METRICS["calendar_held_kill_switch"] += 1
        return IngressResult(202 if not google else 200)
    METRICS["calendar_accepted"] += 1
    queued = await _dedup(channel_id)
    if queued:
        try:
            _enqueue(tenant_id, channel_id, full=full)
        except Exception:  # pragma: no cover - broker down: the poll catches up
            logger.exception("calendar: could not enqueue the delta sync")
    return IngressResult(200 if google else 202, queued=queued)
