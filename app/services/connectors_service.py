"""Facade of the connector framework (Epic 13, AD-29)."""

from app.services.connectors import registry
from app.services.connectors.base import ConnectorConfig, ConnectorPort, ManualActionRequired
from app.services.connectors.jobs import enqueue, pending, retry
from app.services.connectors.registry import get, register_adapter, register_webhook_handler
from app.services.connectors.service import (
    InboundAck,
    create_config,
    ingest_webhook,
    list_configs,
    receive_webhook,
    release_held,
    update_config,
)

__all__ = [
    "ConnectorConfig",
    "ConnectorPort",
    "InboundAck",
    "ManualActionRequired",
    "create_config",
    "enqueue",
    "get",
    "ingest_webhook",
    "list_configs",
    "pending",
    "receive_webhook",
    "register_adapter",
    "register_webhook_handler",
    "registry",
    "release_held",
    "retry",
    "update_config",
]
