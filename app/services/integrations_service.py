# ruff: file-ignore[line-too-long]
"""Integrations facade (Epic 66, spec 0.10; frozen signatures). Installs, configuration and uninstall go through
``marketplace_service``; this facade runs installed connectors. Domain sync handlers (70-72) register with
``register_sync_handler`` and obtain adapters only through ``adapter_for``."""

from app.services.integrations.installed import adapter_for, available_connectors, run_sync, test_connection
from app.services.integrations.links import find_link, link
from app.services.integrations.runner import (
    SyncError,
    SyncHandler,
    SyncResult,
    SyncRun,
    fire_due,
    register_sync_handler,
    unregister_sync_handler,
)

__all__ = [
    "SyncError",
    "SyncHandler",
    "SyncResult",
    "SyncRun",
    "adapter_for",
    "available_connectors",
    "find_link",
    "fire_due",
    "link",
    "register_sync_handler",
    "run_sync",
    "test_connection",
    "unregister_sync_handler",
]
