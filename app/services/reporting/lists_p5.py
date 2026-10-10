# ruff: noqa: E501
"""Phase 5 export lists (Epic 79 story 79.4, FR-413, spec A6).

Three export kinds over the Phase 5 read-model projections (``p5tables``), for kinds the foundation already allows:
``api_usage`` (``usage:read``), ``webhook_deliveries`` (``webhook:manage`` -- never payloads / headers),
``connector_sync`` (``integration:view`` -- never external payloads). The P1 engine limits, formula guard and
``export.run`` audit apply. The data-flow register exports through the console CSV (spec deviation), not here.

Reporting imports **no** Phase 5 module (``p5-below``); these build over ``sqlalchemy.table()`` projections only.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.services.reporting import p5tables
from app.services.reporting.lists import Column

U, W, C = p5tables.usage_counters, p5tables.webhook_deliveries, p5tables.connector_sync_state


class P5ExportFilters(BaseModel):
    """Minimal filter envelope for the Phase 5 export kinds (date range handled by the engine)."""


API_USAGE_COLUMNS: tuple[Column, ...] = (
    Column("day", "Day", "date", U.c.day),
    Column("unit", "Unit", "string", U.c.unit),
    Column("installation_id", "Installation", "uuid", U.c.installation_id),
    Column("count", "Count", "int", U.c.count),
    Column("error_count", "Errors", "int", U.c.error_count),
)

WEBHOOK_DELIVERY_COLUMNS: tuple[Column, ...] = (
    Column("event_type", "Event type", "string", W.c.event_type),
    Column("subscription_id", "Subscription", "uuid", W.c.subscription_id),
    Column("attempt", "Attempt", "int", W.c.attempt),
    Column("status_code", "Status code", "int", W.c.status_code),
    Column("outcome", "Outcome", "string", W.c.outcome),
    Column("created_at", "At", "datetime", W.c.created_at),
)

CONNECTOR_SYNC_COLUMNS: tuple[Column, ...] = (
    Column("installation_id", "Installation", "uuid", C.c.installation_id),
    Column("job_key", "Job", "string", C.c.job_key),
    Column("entity_type", "Entity type", "string", C.c.entity_type),
    Column("status", "Status", "string", C.c.status),
    Column("last_error_code", "Error code", "string", C.c.last_error_code),
)

# key -> (columns, read permission, projection). Registration into the P1 export engine is handled by reporting hooks
# once the Phase 5 projections are present; the kinds themselves are foundation-allowed.
P5_EXPORT_KINDS = {
    "api_usage": (API_USAGE_COLUMNS, "usage:read", U),
    "webhook_deliveries": (WEBHOOK_DELIVERY_COLUMNS, "webhook:manage", W),
    "connector_sync": (CONNECTOR_SYNC_COLUMNS, "integration:view", C),
}


def columns_for(kind: str) -> tuple[Column, ...]:
    return P5_EXPORT_KINDS[kind][0]


__all__ = [
    "API_USAGE_COLUMNS",
    "CONNECTOR_SYNC_COLUMNS",
    "P5_EXPORT_KINDS",
    "WEBHOOK_DELIVERY_COLUMNS",
    "P5ExportFilters",
    "columns_for",
]
