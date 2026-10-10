"""Connector Manifest types (Epic 66, spec 0.9, AD-81; frozen). A manifest describes one platform-authored connector:
category, capabilities, auth, non-secret config model, secret fields, data flows (install notice, FR-335), egress hosts
(FR-337), sync jobs, webhook verification and its adapters per mode (``fake`` required).
"""

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from app.core.integrations.context import AdapterFactory

__all__ = [
    "AdapterMode",
    "AuthType",
    "ConnectorManifest",
    "DataFlow",
    "EmptyConfig",
    "OAuthProviderSpec",
    "SecretField",
    "SyncJobSpec",
    "WebhookSpec",
    "manifest_sha256",
]

AuthType = Literal["oauth2_code", "oauth2_client", "api_key", "basic", "hmac", "user_connection", "file", "none"]
AdapterMode = Literal["fake", "sandbox", "live", "manual"]
ADAPTER_MODES: tuple[str, ...] = ("fake", "sandbox", "live", "manual")


class EmptyConfig(BaseModel):
    """The default (empty) non-secret connector configuration."""

    model_config = ConfigDict(extra="forbid")


@dataclasses.dataclass(frozen=True, slots=True)
class SecretField:
    name: str
    label: str
    kind: Literal["api_key", "password", "token", "private_key", "webhook_secret", "oauth_refresh"]
    required: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class DataFlow:
    """FR-335 data-flow block (install notice basis)."""

    direction: Literal["out", "in"]
    entity: str
    fields: tuple[str, ...]
    purpose: str
    destination_country: str  # ISO-3166 alpha-2, "IN" for India DC
    personal_data: bool


@dataclasses.dataclass(frozen=True, slots=True)
class SyncJobSpec:
    key: str  # ^[a-z][a-z0-9_]{1,59}$
    capability: str  # one of manifest.capabilities
    direction: Literal["push", "pull"]
    trigger: Literal["event", "schedule", "manual", "webhook"]
    event_types: tuple[str, ...] = ()  # outbox types (public or internal) for trigger="event"
    every_seconds: int | None = None  # trigger="schedule"; min 300 (900 for applicant polling)
    entity_type: str = ""  # connector_links.entity_type
    max_batch: int = 100
    per_legal_entity: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class OAuthProviderSpec:
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    pkce: bool = True
    extra_params: Mapping[str, str] = MappingProxyType({})


@dataclasses.dataclass(frozen=True, slots=True)
class WebhookSpec:
    scheme: Literal["hmac_sha256", "hmac_sha1_url", "plivo_v3", "basic", "path_token", "channel_token", "custom"]
    signature_header: str | None = None
    timestamp_header: str | None = None
    tolerance_seconds: int = 300
    event_id_path: str | None = None  # JSON pointer for dedup (connector_inbound_events.external_event_id)
    ip_allowlist: tuple[str, ...] = ()  # CIDRs; required with scheme "path_token"


@dataclasses.dataclass(frozen=True, slots=True)
class ConnectorManifest:
    key: str  # == provider_key, ^[a-z][a-z0-9_]{1,39}$
    version: int
    owner_epic: str
    name: str
    summary: str
    category: str  # in CONNECTOR_CATEGORIES (+ "calendar" listing-only, 72)
    capabilities: frozenset[str]  # in CAPABILITIES
    auth: AuthType
    oauth: OAuthProviderSpec | None = None
    config_model: type[BaseModel] = EmptyConfig  # extra="forbid"; non-secret; JSON Schema drives the UI
    secret_fields: tuple[SecretField, ...] = ()
    data_flows: tuple[DataFlow, ...] = ()
    egress_hosts: tuple[str, ...] = ()  # exact hosts or "*.suffix"; live adapters may reach only these (FR-337)
    tenant_host_field: str | None = None  # config field holding a tenant-specific host
    sync_jobs: tuple[SyncJobSpec, ...] = ()
    webhook: WebhookSpec | None = None  # verify scheme + event kinds
    adapters: "Mapping[AdapterMode, AdapterFactory]" = MappingProxyType({})  # "fake" REQUIRED
    legal_entity_scoped: bool = False
    multi_install: bool = False
    erasure: bool = False  # adapter implements request_erasure (FR-410)
    publisher: Literal["platform"] = "platform"  # in-process connectors are platform-authored only (AD-81)
    docs_url: str | None = None
    rate_per_minute: int = 60  # outbound calls per installation
    sandbox_safe: bool = True  # fake adapter has no network

    def sync_job(self, key: str) -> SyncJobSpec | None:
        return next((j for j in self.sync_jobs if j.key == key), None)

    def webhook_secret_name(self) -> str:
        """The secret field holding the webhook verification secret (default ``webhook_secret``)."""
        return next((f.name for f in self.secret_fields if f.kind == "webhook_secret"), "webhook_secret")


def _consent_view(m: ConnectorManifest) -> dict[str, Any]:
    return {
        "key": m.key,
        "version": m.version,
        "category": m.category,
        "capabilities": sorted(m.capabilities),
        "auth": m.auth,
        "oauth_scopes": sorted(m.oauth.scopes) if m.oauth else [],
        "secret_fields": sorted(f.name for f in m.secret_fields),
        "data_flows": sorted(
            [f.direction, f.entity, sorted(f.fields), f.purpose, f.destination_country, f.personal_data]
            for f in m.data_flows
        ),
        "egress_hosts": sorted(m.egress_hosts),
        "sync_jobs": sorted([j.key, j.capability, j.direction, j.trigger] for j in m.sync_jobs),
        "webhook": m.webhook.scheme if m.webhook else None,
        "erasure": m.erasure,
    }


def manifest_sha256(m: ConnectorManifest) -> str:
    """Stable digest of the consent-relevant manifest content (``app_installations.manifest_sha256``)."""
    blob = json.dumps(_consent_view(m), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
