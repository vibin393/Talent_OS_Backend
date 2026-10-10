# ruff: noqa: E501, D400, D415
"""Connector Conformance Kit (Epic 66, spec 0.9). Runs a registered manifest against its Fake adapter:

* the manifest validates (``core.integrations.validate_manifest``);
* every capability's port methods exist on the Fake and the argument-free ones return a value;
* the Fake resolves in a Sandbox Tenant (``sandbox_safe``) and does no egress outside ``egress_hosts``;
* auth failures are classified (``api_key == "invalid"`` -> ``ConnectorAuthError``);
* a declared webhook verifies and parses a signed body (and refuses a tampered one);
* no secret value appears in ``repr`` / exception messages.

Usage: ``run_conformance(manifest)`` returns a list of problems (``[]`` = conformant). ``make conformance KEY=<key>``
is ``pytest tests/connectors/test_conformance.py -k <key>``. Each connector epic adds live-adapter contract tests with
recorded fixtures (``httpx.MockTransport``), never live calls.
"""

import inspect
import json
import uuid
from typing import Any

import httpx

from app.core.integrations import (
    CAPABILITY_METHODS,
    ConnectorAuthError,
    ConnectorManifest,
    MappingSecretBag,
    is_sandbox_safe,
    make_context,
    validate_manifest,
)
from app.core.integrations.fakes import sign_fake_webhook

SECRET_VALUE = "conformance-secret-value-7f3a"
_NO_ARG_METHODS = frozenset({"field_schema", "languages"})


def _blocked_transport(seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(599)

    return httpx.MockTransport(handler)


def _adapter(manifest: ConnectorManifest, secrets: dict[str, str], seen: list[str]) -> Any:
    ctx = make_context(
        manifest,
        tenant_id=uuid.uuid4(),
        installation_id=uuid.uuid4(),
        config_id=uuid.uuid4(),
        settings={},
        secrets=MappingSecretBag(secrets),
        mode="fake",
        sandbox=True,
        transport=_blocked_transport(seen),
    )
    return manifest.adapters["fake"](ctx)


async def run_conformance(manifest: ConnectorManifest) -> list[str]:
    problems = list(validate_manifest(manifest))
    if "fake" not in manifest.adapters:
        return [*problems, f"{manifest.key}: no fake adapter"]
    if not is_sandbox_safe(manifest.key, {"mode": "fake"}):
        problems.append(f"{manifest.key}: the fake mode is not sandbox-safe")
    seen: list[str] = []
    secrets = {f.name: SECRET_VALUE for f in manifest.secret_fields}
    adapter = _adapter(manifest, secrets, seen)
    if SECRET_VALUE in repr(adapter):
        problems.append(f"{manifest.key}: a secret value appears in the adapter repr")
    for capability in sorted(manifest.capabilities):
        for method in CAPABILITY_METHODS.get(capability, ()):
            fn = getattr(adapter, method, None)
            if fn is None or not callable(fn):
                problems.append(f"{manifest.key}: {capability} needs {method}()")
                continue
            if method in _NO_ARG_METHODS and inspect.iscoroutinefunction(fn):
                try:
                    if await fn() is None:
                        problems.append(f"{manifest.key}: {method}() returned None")
                except Exception as exc:  # pragma: no cover - reported, not raised
                    problems.append(f"{manifest.key}: {method}() raised {type(exc).__name__}")
                    if SECRET_VALUE in str(exc):
                        problems.append(f"{manifest.key}: a secret value leaked into an exception")
    # auth-failure classification
    bad = _adapter(manifest, {**secrets, "api_key": "invalid"}, seen)
    probe = next(
        (
            m
            for cap in sorted(manifest.capabilities)
            for m in CAPABILITY_METHODS.get(cap, ())
            if m in _NO_ARG_METHODS
        ),
        None,
    )
    if probe is not None and any(f.name == "api_key" for f in manifest.secret_fields):
        try:
            await getattr(bad, probe)()
            problems.append(f"{manifest.key}: an invalid api_key was not refused")
        except ConnectorAuthError as exc:
            if SECRET_VALUE in str(exc):
                problems.append(f"{manifest.key}: a secret value leaked into an auth error")
        except Exception as exc:
            problems.append(
                f"{manifest.key}: an invalid api_key raised {type(exc).__name__}, not ConnectorAuthError"
            )
    # webhook round trip
    if manifest.webhook is not None and hasattr(adapter, "verify_webhook"):
        body = json.dumps({"event_id": "evt-1", "kind": "ping"}).encode()
        headers = sign_fake_webhook(SECRET_VALUE, body)
        if not adapter.verify_webhook(headers, body):
            problems.append(f"{manifest.key}: a correctly signed webhook was refused")
        if adapter.verify_webhook(headers, body + b" "):
            problems.append(f"{manifest.key}: a tampered webhook was accepted")
        if dict(adapter.parse_webhook(headers, body)).get("event_id") != "evt-1":
            problems.append(f"{manifest.key}: parse_webhook lost the event id")
    allowed = set(manifest.egress_hosts)
    outside = sorted({h for h in seen if h not in allowed})
    if outside:
        problems.append(f"{manifest.key}: egress outside egress_hosts: {outside}")
    return problems


__all__ = ["SECRET_VALUE", "run_conformance"]
