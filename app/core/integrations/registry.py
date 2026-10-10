"""Connector Manifest registry (Epic 66, spec 0.9, AD-81). Owners call ``register_connector`` from their ``hooks.py``;
``validate_catalogue`` runs after hooks autodiscovery (startup fails on any error).

Core never imports a service: ``app.services.connectors.registry`` subscribes through ``on_register`` and registers each
manifest's adapters with the legacy adapter registry (``(category, key)`` plus ``(category, "fake" | "manual")`` when
those keys are free) as ``BridgedFactory`` objects -- callable with a legacy ``ConnectorConfig`` and exposing
``from_context(AdapterContext)``.
"""

import inspect
import re
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from app.core.integrations.capabilities import (
    CAPABILITIES,
    CAPABILITY_METHODS,
    MANIFEST_CATEGORIES,
    PERSONAL_DATA_CAPABILITIES,
)
from app.core.integrations.context import AdapterContext, MappingSecretBag, make_context, resolve_mode
from app.core.integrations.errors import ManifestError
from app.core.integrations.manifest import ADAPTER_MODES, ConnectorManifest

__all__ = [
    "BridgedFactory",
    "find_manifest",
    "is_sandbox_safe",
    "manifest",
    "manifests",
    "on_register",
    "register_connector",
    "unregister_connector",
    "validate_catalogue",
    "validate_manifest",
]

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_JOB_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,59}$")
_NIL = uuid.UUID(int=0)
MIN_SCHEDULE_SECONDS = 300

_manifests: dict[str, ConnectorManifest] = {}
_listeners: list[Callable[[ConnectorManifest], None]] = []
_duplicates: list[str] = []


class BridgedFactory:
    """A manifest adapter factory usable by the legacy ``connectors.registry`` (``factory(ConnectorConfig | None)``).

    ``mode`` pins the adapter mode (the ``fake`` / ``manual`` provider keys); ``None`` resolves it from the config's
    ``settings["mode"]``. The legacy path builds a context from the config's decrypted credentials."""

    def __init__(self, manifest: ConnectorManifest, mode: str | None = None) -> None:
        self.manifest = manifest
        self.mode = mode

    def __repr__(self) -> str:
        return f"BridgedFactory({self.manifest.key!r}, mode={self.mode!r})"

    def from_context(self, ctx: AdapterContext) -> Any:
        factory = self.manifest.adapters.get(ctx.mode) or self.manifest.adapters.get("fake")
        if factory is None:
            msg = f"connector {self.manifest.key!r} has no {ctx.mode!r} adapter"
            raise ManifestError(msg)
        return factory(ctx)

    def __call__(self, config: Any = None) -> Any:
        settings: dict[str, Any] = dict(getattr(config, "settings", None) or {})
        secrets: dict[str, Any] = dict(getattr(config, "credentials", None) or {})
        webhook_secret = getattr(config, "webhook_secret", None)
        if webhook_secret:
            secrets.setdefault(self.manifest.webhook_secret_name(), webhook_secret)
        mode = self.mode if self.mode is not None else (settings.get("mode") if config is not None else "manual")
        if mode == "manual" and "manual" not in self.manifest.adapters:
            mode = None
        ctx = make_context(
            self.manifest,
            tenant_id=getattr(config, "tenant_id", None) or _NIL,
            installation_id=getattr(config, "installation_id", None),
            config_id=getattr(config, "id", None),
            settings=settings,
            secrets=MappingSecretBag(secrets),
            mode=mode,
        )
        return self.from_context(ctx)


def on_register(fn: Callable[[ConnectorManifest], None]) -> None:
    """Call ``fn`` for every registered manifest (existing ones immediately, later ones on registration)."""
    if fn in _listeners:
        return
    _listeners.append(fn)
    for m in list(_manifests.values()):
        fn(m)


def register_connector(manifest: ConnectorManifest) -> None:
    """Register a manifest (idempotent for the same object / equal content; a different manifest under an existing
    key is recorded as a catalogue error). Structural errors raise ``ManifestError`` at once."""
    if not isinstance(manifest, ConnectorManifest):
        msg = f"not a ConnectorManifest: {manifest!r}"
        raise ManifestError(msg)
    if not _KEY_RE.match(manifest.key):
        msg = f"invalid connector key {manifest.key!r}"
        raise ManifestError(msg)
    existing = _manifests.get(manifest.key)
    if existing is not None and existing is not manifest:
        if (existing.owner_epic, existing.category, existing.version) != (
            manifest.owner_epic,
            manifest.category,
            manifest.version,
        ):
            _duplicates.append(f"{manifest.key}: registered twice ({existing.owner_epic} / {manifest.owner_epic})")
            return
    _manifests[manifest.key] = manifest
    for fn in list(_listeners):
        fn(manifest)


def unregister_connector(key: str) -> None:
    """Tests only."""
    _manifests.pop(key, None)


def manifest(key: str) -> ConnectorManifest:
    try:
        return _manifests[key]
    except KeyError:
        msg = f"unknown connector {key!r}"
        raise KeyError(msg) from None


def find_manifest(key: str | None) -> ConnectorManifest | None:
    return _manifests.get(key) if key else None


def manifests(category: str | None = None) -> list[ConnectorManifest]:
    return sorted(
        (m for m in _manifests.values() if category is None or m.category == category), key=lambda m: m.key
    )


def is_sandbox_safe(provider_key: str, settings: Mapping[str, Any] | None = None) -> bool:
    """True when the configuration resolves to a ``fake`` / ``manual`` adapter (allowed in a Sandbox Tenant)."""
    if provider_key in ("fake", "manual"):
        return True
    m = _manifests.get(provider_key)
    if m is None:
        return False
    requested = (settings or {}).get("mode")
    return resolve_mode(m, requested, sandbox=False) in ("fake", "manual")


# ---------------------------------------------------------------------------------------------- validation
def _listing_only(m: ConnectorManifest) -> bool:
    return m.auth == "user_connection" and not m.adapters


def _missing_methods(factory: Any, capabilities: frozenset[str]) -> list[str]:
    target = factory if inspect.isclass(factory) else None
    if target is None:
        return []  # plain callables are checked by the Conformance Kit (instantiated with a context)
    missing: list[str] = []
    for cap in sorted(capabilities):
        for method in CAPABILITY_METHODS.get(cap, ()):
            if not callable(getattr(target, method, None)):
                missing.append(f"{cap}:{method}")
    for method in ("handle", "verify_webhook", "parse_webhook"):
        if not callable(getattr(target, method, None)):
            missing.append(f"port:{method}")
    return missing


def validate_manifest(m: ConnectorManifest) -> list[str]:
    errs: list[str] = []
    p = f"{m.key}:"
    if not _KEY_RE.match(m.key):
        errs.append(f"{p} invalid key")
    if m.version < 1:
        errs.append(f"{p} version must be >= 1")
    if m.category not in MANIFEST_CATEGORIES:
        errs.append(f"{p} unknown category {m.category!r}")
    unknown = sorted(c for c in m.capabilities if c not in CAPABILITIES)
    if unknown:
        errs.append(f"{p} unknown capabilities {unknown}")
    if m.publisher != "platform":
        errs.append(f"{p} in-process connectors are platform-authored only")
    bad_modes = sorted(k for k in m.adapters if k not in ADAPTER_MODES)
    if bad_modes:
        errs.append(f"{p} unknown adapter modes {bad_modes}")
    if not _listing_only(m):
        fake = m.adapters.get("fake")
        if fake is None:
            errs.append(f"{p} a fake adapter is required")
        else:
            missing = _missing_methods(fake, frozenset(c for c in m.capabilities if c in CAPABILITIES))
            if missing:
                errs.append(f"{p} fake adapter lacks {missing}")
    if ("live" in m.adapters or "sandbox" in m.adapters) and not m.egress_hosts:
        errs.append(f"{p} live / sandbox adapters need egress_hosts")
    extra = m.config_model.model_config.get("extra")
    if extra != "forbid":
        errs.append(f"{p} config_model must forbid extra fields")
    fields = set(m.config_model.model_fields)
    clash = sorted(f.name for f in m.secret_fields if f.name in fields)
    if clash:
        errs.append(f"{p} secret fields in config_model {clash}")
    names = [f.name for f in m.secret_fields]
    if len(names) != len(set(names)):
        errs.append(f"{p} duplicate secret field names")
    if m.tenant_host_field:
        if m.tenant_host_field not in fields:
            errs.append(f"{p} tenant_host_field {m.tenant_host_field!r} is not a config field")
        if not any(h.startswith("*.") for h in m.egress_hosts):
            errs.append(f"{p} tenant_host_field needs a '*.suffix' egress host")
    personal = sorted(c for c in m.capabilities if c in PERSONAL_DATA_CAPABILITIES)
    if personal and not any(f.personal_data for f in m.data_flows):
        errs.append(f"{p} personal-data capabilities {personal} need a personal-data DataFlow")
    for flow in m.data_flows:
        if len(flow.destination_country) != 2 or not flow.destination_country.isupper():
            errs.append(f"{p} data flow {flow.entity!r} destination_country must be ISO-3166 alpha-2")
    if m.auth == "oauth2_code" and m.oauth is None:
        errs.append(f"{p} auth oauth2_code needs an OAuthProviderSpec")
    seen: set[str] = set()
    for job in m.sync_jobs:
        jp = f"{p} sync job {job.key!r}"
        if not _JOB_KEY_RE.match(job.key):
            errs.append(f"{jp} invalid key")
        if job.key in seen:
            errs.append(f"{jp} duplicate")
        seen.add(job.key)
        if job.capability not in m.capabilities:
            errs.append(f"{jp} capability {job.capability!r} not declared")
        if job.trigger == "event" and not job.event_types:
            errs.append(f"{jp} trigger 'event' needs event_types")
        if job.trigger == "schedule" and (job.every_seconds is None or job.every_seconds < MIN_SCHEDULE_SECONDS):
            errs.append(f"{jp} schedule needs every_seconds >= {MIN_SCHEDULE_SECONDS}")
        if job.max_batch < 1:
            errs.append(f"{jp} max_batch must be >= 1")
    if m.webhook is not None and m.webhook.scheme == "path_token" and not m.webhook.ip_allowlist:
        errs.append(f"{p} webhook scheme path_token needs an ip_allowlist")
    if m.rate_per_minute < 1:
        errs.append(f"{p} rate_per_minute must be >= 1")
    return errs


def validate_catalogue() -> list[str]:
    """Post-discovery validation of every registered manifest (``[]`` = valid)."""
    errs = list(_duplicates)
    for m in manifests():
        errs.extend(validate_manifest(m))
    return errs
