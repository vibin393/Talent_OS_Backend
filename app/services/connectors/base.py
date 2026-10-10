"""Provider connector framework: the generic port (AD-29, NFR-31).

Every adapter (``<category>/manual.py``, ``fake.py``, real providers) implements ``ConnectorPort``: ``handle`` runs
one outbound operation for ``connector_jobs``; ``verify_webhook`` / ``parse_webhook`` serve the public webhook
endpoint. Category ports (``<category>/port.py``) add the typed methods of the owning epic. Only adapter files
import a provider SDK / client.
"""

import dataclasses
import uuid
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from app.core.integrations.errors import ManualActionRequired as _CoreManualActionRequired


@dataclasses.dataclass(frozen=True)
class ConnectorConfig:
    """A provider configuration with its decrypted credentials (never logged, never returned by an API)."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    category: str
    provider_key: str
    legal_entity_id: uuid.UUID | None
    settings: Mapping[str, Any]
    credentials: Mapping[str, Any] | None
    webhook_secret: str | None

    def __repr__(self) -> str:  # never print credentials
        return f"ConnectorConfig(id={self.id}, category={self.category}, provider_key={self.provider_key})"


@runtime_checkable
class ConnectorPort(Protocol):
    category: str
    provider_key: str

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool: ...

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]: ...


# Epic 66: one class for legacy and manifest adapters (``core.integrations.errors``), caught everywhere.
ManualActionRequired = _CoreManualActionRequired
