"""Fake ``payroll`` connector for tests (AD-29): records calls, returns deterministic results, verifies
webhooks with HMAC-SHA256 of the body (``X-Fake-Signature``) using the configured webhook secret.
Registered as provider ``fake`` outside production."""

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from app.core.settings import get_settings
from app.services.connectors.base import ConnectorConfig
from app.services.connectors.registry import register_adapter


class FakePayrollProvider:
    category = "payroll"
    provider_key = "fake"
    key = "fake"
    calls: ClassVar[list[tuple[str, Any]]] = []
    fail_ops: ClassVar[set[str]] = set()

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()
        cls.fail_ops.clear()

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        type(self).calls.append((op, dict(payload)))
        if op in type(self).fail_ops:
            msg = f"fake {op} failure"
            raise RuntimeError(msg)
        digest = hashlib.sha256(json.dumps(dict(payload), sort_keys=True, default=str).encode()).hexdigest()
        return {"ok": True, "op": op, "ref": f"fake-{digest[:12]}"}

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        secret = self.config.webhook_secret if self.config is not None else None
        if not secret:
            return False
        given = {k.lower(): v for k, v in headers.items()}.get("x-fake-signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(given, expected)

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        try:
            data = json.loads(body or b'{}')
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"data": data}

    async def push_export(self, export_id: uuid.UUID, file: bytes, media_type: str) -> Mapping[str, Any]:
        """Epic 66 ``PayrollPushPort``: idempotent per (export, file digest)."""
        type(self).calls.append(("push_export", (export_id, len(file), media_type)))
        if "push_export" in type(self).fail_ops:
            msg = "fake push_export failure"
            raise RuntimeError(msg)
        digest = hashlib.sha256(str(export_id).encode() + file).hexdigest()
        return {"ok": True, "external_ref": f"fake-payroll-{digest[:12]}", "bytes": len(file), "media_type": media_type}

    async def get_status(self, external_ref: str) -> Any:
        type(self).calls.append(("get_status", (external_ref,)))
        return {"ok": True, "op": "get_status"}


if get_settings().app_env != "production":
    register_adapter("payroll", "fake", FakePayrollProvider)

__all__ = ["FakePayrollProvider", "Sequence"]
