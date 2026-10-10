# ruff: noqa: E501
"""Phase 5 read-model projections for reporting (Epic 79, AD-1 read-model exception).

Read-only ``sqlalchemy.table()`` projections guarded by ``available()``; reporting imports **no** Phase 5 module
(``p5-below``). Tiles that need platform tables (Job Network) come through ``ecosystem_governance.dashboards`` which
calls ``job_network_service.tenant_stats`` -- not from here.
"""

from __future__ import annotations

import sqlalchemy as sa

# Usage counters (Epic 66) -- API / webhook / connector / telephony units per day per installation.
usage_counters = sa.table(
    "usage_counters",
    sa.column("tenant_id", sa.Uuid),
    sa.column("day", sa.Date),
    sa.column("unit", sa.String),
    sa.column("installation_id", sa.Uuid),
    sa.column("api_key_id", sa.Uuid),
    sa.column("count", sa.Integer),
    sa.column("error_count", sa.Integer),
)

# Webhook deliveries (Epic 68) -- outcome + latency only; never payloads or headers.
webhook_deliveries = sa.table(
    "webhook_deliveries",
    sa.column("tenant_id", sa.Uuid),
    sa.column("subscription_id", sa.Uuid),
    sa.column("event_type", sa.String),
    sa.column("attempt", sa.Integer),
    sa.column("status_code", sa.Integer),
    sa.column("outcome", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
)

# Connector sync state (Epic 66) -- job key, entity type, status, error code; never external payloads.
connector_sync_state = sa.table(
    "connector_sync_state",
    sa.column("tenant_id", sa.Uuid),
    sa.column("installation_id", sa.Uuid),
    sa.column("job_key", sa.String),
    sa.column("entity_type", sa.String),
    sa.column("status", sa.String),
    sa.column("last_error_code", sa.String),
    sa.column("consecutive_failures", sa.Integer),
)

# App installations (Epic 66) -- health projection for the connector-health tile.
app_installations = sa.table(
    "app_installations",
    sa.column("tenant_id", sa.Uuid),
    sa.column("id", sa.Uuid),
    sa.column("kind", sa.String),
    sa.column("connector_key", sa.String),
    sa.column("status", sa.String),
    sa.column("health_status", sa.String),
)


def available(name: str, inspector_tables: set[str]) -> bool:
    """Whether the projected table exists in this database (reporting stays route/table-existence based)."""
    return name in inspector_tables


__all__ = [
    "app_installations",
    "available",
    "connector_sync_state",
    "usage_counters",
    "webhook_deliveries",
]
