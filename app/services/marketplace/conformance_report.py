# ruff: file-ignore[line-too-long, missing-trailing-period, missing-terminal-punctuation]
"""Conformance report verification (Epic 69, spec A8). Reports are produced by Epic 74's sandbox runner and signed with
the platform secret ``conformance_report_signing`` (``vault_service.platform_get``):

``signature = "v1=" + hex(HMAC-SHA256(key, canonical JSON of the report without "signature"))`` where the canonical
JSON is ``json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False)``.
"""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services import vault_service

SCHEMA = "talentos.conformance/1"
SIGNING_SECRET = "conformance_report_signing"
MAX_AGE = timedelta(days=30)
REQUIRED_CHECKS = (
    "oauth_client_credentials",
    "oauth_authorization_code_pkce",
    "scope_minimality",
    "webhook_signature_verified",
    "idempotent_retry",
    "handles_429",
    "uninstall_cleanup",
)


def canonical(report: dict[str, Any]) -> bytes:
    body = {k: v for k, v in report.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def sign(report: dict[str, Any], key: str) -> str:
    return "v1=" + hmac.new(key.encode(), canonical(report), hashlib.sha256).hexdigest()


async def signing_key() -> str | None:
    try:
        return await vault_service.platform_get(SIGNING_SECRET, purpose="marketplace:conformance_report")
    except vault_service.SecretNotFound:
        return None


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def verify(
    report: dict[str, Any] | None,
    *,
    app_id: uuid.UUID | None = None,
    manifest_sha256: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Problems with a report (``[]`` = valid): schema, signature, ``passed``, required checks, App / manifest match and
    a finish time no older than 30 days."""
    if not report:
        return ["conformance_report_missing"]
    problems: list[str] = []
    if report.get("schema") != SCHEMA:
        problems.append("schema_unknown")
    key = await signing_key()
    signature = str(report.get("signature") or "")
    if key is None:
        problems.append("signing_key_missing")
    elif not hmac.compare_digest(signature, sign(report, key)):
        problems.append("signature_invalid")
    if report.get("passed") is not True:
        problems.append("not_passed")
    checks = {c.get("key"): bool(c.get("passed")) for c in report.get("checks") or [] if isinstance(c, dict)}
    missing = [k for k in REQUIRED_CHECKS if not checks.get(k)]
    if missing:
        problems.append("checks_failed:" + ",".join(missing))
    if app_id is not None and str(report.get("app_id")) != str(app_id):
        problems.append("app_mismatch")
    if manifest_sha256 is not None and report.get("manifest_sha256") != manifest_sha256:
        problems.append("manifest_mismatch")
    finished = _parse(report.get("finished_at"))
    current = now or datetime.now(UTC)
    if finished is None:
        problems.append("finished_at_missing")
    elif current - finished > MAX_AGE:
        problems.append("expired")
    return problems


__all__ = [
    "MAX_AGE",
    "REQUIRED_CHECKS",
    "SCHEMA",
    "SIGNING_SECRET",
    "canonical",
    "sign",
    "signing_key",
    "verify",
]
