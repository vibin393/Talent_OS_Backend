# ruff: file-ignore[line-too-long]
"""Facade of the ``public_events`` domain (Epic 66, AD-79, spec §0.8; frozen API consumed by Epic 68).

* ``register_sink`` / ``SinkFn`` -- outbox consumers (``connector_triggers`` 66, ``webhooks`` 68, ...); idempotent.
* ``dispatch`` -- the relay task body; ``sweep_tenant`` -- crash recovery; ``purge`` -- retention.
* ``list_events`` / ``get_event`` -- the replay / backfill read model; ``envelope`` -- the canonical wire bytes;
  ``tenant_ref(db, tenant_id) -> (slug, livemode)`` feeds it.
"""

import uuid

from app.core.database import tenant_session
from app.services.public_events.dispatch import dispatch, has_stalled, sweep_tenant
from app.services.public_events.outbox import (
    API_VERSION,
    OutboxRow,
    PublicEventRow,
    envelope,
    envelope_dict,
    get_event,
    list_events,
    tenant_ref,
)
from app.services.public_events.outbox import purge as _purge
from app.services.public_events.sinks import SinkFn, register_sink, sinks, unregister_sink


async def purge(tenant_id: uuid.UUID, *, older_than_days: int) -> int:
    """Delete the tenant's dispatched outbox rows older than ``older_than_days``."""
    async with tenant_session(tenant_id) as db:
        return await _purge(db, older_than_days=older_than_days)


__all__ = [
    "API_VERSION",
    "OutboxRow",
    "PublicEventRow",
    "SinkFn",
    "dispatch",
    "envelope",
    "envelope_dict",
    "get_event",
    "has_stalled",
    "list_events",
    "purge",
    "register_sink",
    "sinks",
    "sweep_tenant",
    "tenant_ref",
    "unregister_sink",
]
