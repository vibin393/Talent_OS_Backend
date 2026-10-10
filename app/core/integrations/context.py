"""Adapter context (Epic 66, spec 0.9): what every manifest adapter factory receives.

``SecretBag`` is a protocol here (``get(name) -> str | None``); the vault ships the concrete, logged bag
(``vault_service.open_bag``). ``MappingSecretBag`` serves legacy (Fernet) configs and tests. ``connector_http``
builds the Egress-Guarded client for a manifest (``allowed_hosts = egress_hosts`` + the tenant host field,
``max_redirects=3``, 1 MB cap).
"""

import dataclasses
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel

from app.core.integrations.manifest import ADAPTER_MODES, AdapterMode, ConnectorManifest
from app.core.net_safety import SafeHttpClient

__all__ = [
    "AdapterContext",
    "AdapterFactory",
    "MappingSecretBag",
    "SecretBag",
    "allowed_hosts_for",
    "connector_http",
    "make_context",
    "resolve_mode",
]


@runtime_checkable
class SecretBag(Protocol):
    def get(self, name: str) -> str | None: ...


class MappingSecretBag:
    """An in-memory secret bag (legacy configs, tests). Never printed, never serialised."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        self._values = {str(k): str(v) for k, v in (values or {}).items() if v is not None}

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))

    def __repr__(self) -> str:
        return "SecretBag(****)"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        msg = "SecretBag cannot be pickled"
        raise TypeError(msg)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclasses.dataclass(frozen=True)
class AdapterContext:
    tenant_id: uuid.UUID
    installation_id: uuid.UUID | None
    config_id: uuid.UUID | None
    mode: AdapterMode
    config: BaseModel  # validated config_model
    secrets: SecretBag  # lazy, logged reads (vault); repr "SecretBag(****)"
    http: SafeHttpClient  # Egress Guard + manifest egress_hosts + timeouts + size caps
    now: Callable[[], datetime] = _utcnow
    sandbox: bool = False

    def __repr__(self) -> str:
        return (
            f"AdapterContext(tenant_id={self.tenant_id}, installation_id={self.installation_id}, "
            f"config_id={self.config_id}, mode={self.mode}, sandbox={self.sandbox})"
        )


AdapterFactory = Callable[[AdapterContext], Any]  # returns an object implementing ConnectorPort + category ports


def resolve_mode(manifest: ConnectorManifest, requested: str | None, *, sandbox: bool) -> AdapterMode:
    """The adapter mode for a configuration: the requested mode when the manifest has it, else the first of
    live / sandbox / fake / manual it has. A Sandbox Tenant only ever gets ``fake`` or ``manual`` (FR-339)."""
    adapters = manifest.adapters
    mode: str | None = requested if requested in adapters else None
    if mode is None:
        mode = next((m for m in ("live", "sandbox", "fake", "manual") if m in adapters), "fake")
    if sandbox and mode not in ("fake", "manual"):
        mode = "fake"
    assert mode in ADAPTER_MODES
    return mode  # type: ignore[return-value]


def allowed_hosts_for(manifest: ConnectorManifest, config: BaseModel | Mapping[str, Any] | None) -> list[str]:
    """``egress_hosts`` + the tenant-specific host (only when it falls under a ``*.suffix`` egress pattern)."""
    hosts = list(manifest.egress_hosts)
    field = manifest.tenant_host_field
    if field and config is not None:
        value = config.get(field) if isinstance(config, Mapping) else getattr(config, field, None)
        if isinstance(value, str) and value:
            host = value.strip().lower().rstrip(".")
            for pattern in manifest.egress_hosts:
                if pattern.startswith("*.") and host.endswith(pattern[1:]) and host != pattern[2:]:
                    hosts.append(host)
                    break
    return hosts


def connector_http(
    manifest: ConnectorManifest,
    config: BaseModel | Mapping[str, Any] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SafeHttpClient:
    return SafeHttpClient(
        allowed_hosts=allowed_hosts_for(manifest, config),
        max_redirects=3,
        max_bytes=1_048_576,
        purpose=f"connector:{manifest.key}",
        transport=transport,
    )


def make_context(
    manifest: ConnectorManifest,
    *,
    tenant_id: uuid.UUID,
    installation_id: uuid.UUID | None,
    config_id: uuid.UUID | None,
    settings: Mapping[str, Any] | None,
    secrets: SecretBag,
    mode: str | None = None,
    sandbox: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], datetime] | None = None,
) -> AdapterContext:
    """Validate ``settings`` against ``config_model`` (the ``mode`` key selects the adapter mode) and build the
    context. Raises ``pydantic.ValidationError`` on an invalid configuration."""
    raw = dict(settings or {})
    requested = raw.pop("mode", None)
    config = manifest.config_model.model_validate(raw)
    resolved = resolve_mode(manifest, mode or requested, sandbox=sandbox)
    return AdapterContext(
        tenant_id=tenant_id,
        installation_id=installation_id,
        config_id=config_id,
        mode=resolved,
        config=config,
        secrets=secrets,
        http=connector_http(manifest, config, transport=transport),
        now=now or _utcnow,
        sandbox=sandbox,
    )
