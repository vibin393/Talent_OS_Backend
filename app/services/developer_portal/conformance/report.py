# ruff: file-ignore[line-too-long]
"""The signed conformance report (Epic 74, spec 0.6; format and signing are Epic 69's ``conformance_report``):
``talentos.conformance/1`` with the seven required checks, ``observed_scopes`` / ``observed_hosts``, signed
``v1=<hex HMAC-SHA256>`` over the canonical JSON without ``signature`` using platform secret
``conformance_report_signing``. Reports carry no secrets and no partner data beyond the observations."""

from datetime import datetime
from typing import Any

from app.core.errors import DomainError
from app.services.marketplace import conformance_report

KIT_VERSION = "74.1"


async def build(
    *, run_id: str, app_id: str, manifest_sha256: str, sandbox_ref: str, started_at: datetime, finished_at: datetime,
    checks: list[dict[str, Any]], observed_scopes: list[str], observed_hosts: list[str],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": conformance_report.SCHEMA, "kit_version": KIT_VERSION, "run_id": run_id, "app_id": app_id,
        "manifest_sha256": manifest_sha256, "sandbox_tenant_ref": sandbox_ref, "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(), "passed": all(c["passed"] for c in checks), "checks": checks,
        "observed_scopes": sorted(set(observed_scopes)), "observed_hosts": sorted(set(observed_hosts)),
    }  # fmt: skip
    key = await conformance_report.signing_key()
    if key is None:
        exc = DomainError("Conformance reports cannot be signed right now", code="signing_key_missing")
        exc.status_code = 503
        raise exc
    report["signature"] = conformance_report.sign(report, key)
    return report


__all__ = ["KIT_VERSION", "build"]
