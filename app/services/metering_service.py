"""Usage metering facade (Epic 66, spec 0.12; frozen signatures). Informational only (D-12)."""

from app.services.metering.enforce import clear_tier_cache, enforce, resolve_tier
from app.services.metering.flush import flush
from app.services.metering.partitions import (
    ensure_monthly,
    purge,
    register_partitioned_table,
    registered_tables,
)
from app.services.metering.read import platform_usage, usage
from app.services.metering.reconcile import Drift, reconcile
from app.services.metering.record import record
from app.services.metering.request_log import register_request_log

purge_request_log = purge

__all__ = [
    "Drift",
    "clear_tier_cache",
    "enforce",
    "ensure_monthly",
    "flush",
    "platform_usage",
    "purge",
    "purge_request_log",
    "reconcile",
    "record",
    "register_partitioned_table",
    "register_request_log",
    "registered_tables",
    "resolve_tier",
    "usage",
]
