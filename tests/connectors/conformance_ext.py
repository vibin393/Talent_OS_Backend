# ruff: noqa: E501
"""Conformance Kit extension (Epic 69, spec A8): extra checks run over every registered manifest.

* ``manual_fallback``: the manifest's ``manual`` adapter (when declared) raises ``ManualActionRequired`` with non-empty
  instructions for every capability op;
* ``retry_classified``: a Fake failing an op raises ``ConnectorTransientError`` (the job layer retries it with its
  back-off; ``dead`` after ``max_attempts``);
* ``rate_limited_classified``: a Fake scripted to rate-limit raises ``ConnectorRateLimited`` (the job layer reschedules
  without counting an attempt).

``uninstall_cleanup`` (no vault rows, configs + sync state disabled, no queued jobs after uninstall) is exercised against
the database in ``tests/marketplace/test_install.py``.
"""

import uuid
from typing import Any

from app.core.integrations import (
    CAPABILITY_METHODS,
    ConnectorManifest,
    ConnectorRateLimited,
    ConnectorTransientError,
    ManualActionRequired,
    MappingSecretBag,
    make_context,
)
from app.core.integrations.fakes import FakeAdapterBase


def _ctx(manifest: ConnectorManifest, mode: str) -> Any:
    return make_context(
        manifest, tenant_id=uuid.uuid4(), installation_id=uuid.uuid4(), config_id=uuid.uuid4(), settings={},
        secrets=MappingSecretBag({f.name: "conformance-ext" for f in manifest.secret_fields}), mode=mode, sandbox=True,
    )  # fmt: skip


async def run_conformance_ext(manifest: ConnectorManifest) -> list[str]:
    problems: list[str] = []
    ops = sorted({m for cap in manifest.capabilities for m in CAPABILITY_METHODS.get(cap, ())})
    if "manual" in manifest.adapters:
        manual = manifest.adapters["manual"](_ctx(manifest, "manual"))
        for op in ops:
            try:
                await manual.handle(op, {})
                problems.append(f"{manifest.key}: manual {op} did not ask for a manual action")
            except ManualActionRequired as exc:
                if not str(exc).strip():
                    problems.append(f"{manifest.key}: manual {op} gave no instructions")
            except Exception as exc:
                problems.append(f"{manifest.key}: manual {op} raised {type(exc).__name__}")
    fake_factory = manifest.adapters.get("fake")
    if isinstance(fake_factory, type) and issubclass(fake_factory, FakeAdapterBase):
        fake = fake_factory(_ctx(manifest, "fake"))
        cls = type(fake)
        try:
            cls.fail_ops.add("health")
            try:
                await fake.handle("health", {})
                problems.append(f"{manifest.key}: a failing op did not raise")
            except ConnectorTransientError:
                pass
            except Exception as exc:
                problems.append(
                    f"{manifest.key}: a failing op raised {type(exc).__name__}, not ConnectorTransientError"
                )
            cls.fail_ops.discard("health")
            cls.script["rate_limit_ops"] = ["health"]
            try:
                await fake.handle("health", {})
                problems.append(f"{manifest.key}: a rate-limited op did not raise")
            except ConnectorRateLimited:
                pass
            except Exception as exc:
                problems.append(f"{manifest.key}: a rate-limited op raised {type(exc).__name__}")
        finally:
            cls.reset()
    return problems


__all__ = ["run_conformance_ext"]
