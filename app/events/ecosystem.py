# ruff: file-ignore[line-too-long]
"""Ecosystem events (Epic 66, spec 0.4): Installations, the Ecosystem Kill Switch, OAuth token families, delegated
authorisations and connector sync / health.

Contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids and scalars only (no PII); dispatched
after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = [
    "ConnectorHealthChanged",
    "ConnectorSyncFailed",
    "EcosystemKillSwitchChanged",
    "InstallationChanged",
    "OAuthTokenFamilyRevoked",
    "UserGrantChanged",
]


@dataclasses.dataclass(frozen=True, slots=True)
class InstallationChanged:
    """``change`` in installed | granted | upgraded | config_changed | suspended | resumed | degraded |
    reconsent_required | uninstalled."""

    tenant_id: uuid.UUID
    installation_id: uuid.UUID
    kind: str
    app_id: uuid.UUID | None
    connector_key: str | None
    change: str
    status: str
    actor_kind: str


@dataclasses.dataclass(frozen=True, slots=True)
class EcosystemKillSwitchChanged:
    """``scope`` in tenant | platform_tenant | platform_app | global (global / app changes are emitted per tenant)."""

    tenant_id: uuid.UUID
    scope: str
    on: bool
    app_id: uuid.UUID | None
    actor_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class OAuthTokenFamilyRevoked:
    tenant_id: uuid.UUID
    installation_id: uuid.UUID
    family_id: uuid.UUID
    user_id: uuid.UUID | None
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class UserGrantChanged:
    """``resource`` api | mcp; ``change`` granted | revoked."""

    tenant_id: uuid.UUID
    installation_id: uuid.UUID
    user_id: uuid.UUID
    resource: str
    change: str


@dataclasses.dataclass(frozen=True, slots=True)
class ConnectorSyncFailed:
    tenant_id: uuid.UUID
    installation_id: uuid.UUID
    config_id: uuid.UUID | None
    job_key: str
    error_code: str
    consecutive_failures: int


@dataclasses.dataclass(frozen=True, slots=True)
class ConnectorHealthChanged:
    tenant_id: uuid.UUID
    installation_id: uuid.UUID
    health_status: str
    error_code: str | None
