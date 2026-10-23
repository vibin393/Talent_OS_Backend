"""Fake ``einvoice`` connector for tests (AD-29): records calls, returns deterministic results, verifies
webhooks with HMAC-SHA256 of the body (``X-Fake-Signature``) using the configured webhook secret.
Registered as provider ``fake`` outside production."""

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from app.core.settings import get_settings
from app.services.connectors.base import ConnectorConfig
from app.services.connectors.registry import register_adapter


class FakeEInvoiceProvider:
    category = "einvoice"
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

    async def generate_irn(self, doc_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        type(self).calls.append(("generate_irn", (doc_payload,)))
        return {"ok": True, "op": "generate_irn"}

    async def cancel_irn(self, irn: str, reason: str) -> Mapping[str, Any]:
        type(self).calls.append(("cancel_irn", (irn, reason)))
        return {"ok": True, "op": "cancel_irn"}

    async def get_irn(self, doc_ref: str) -> Mapping[str, Any] | None:
        type(self).calls.append(("get_irn", (doc_ref,)))
        return {"ok": True, "op": "get_irn"}


if get_settings().app_env != "production":
    register_adapter("einvoice", "fake", FakeEInvoiceProvider)

__all__ = ["FakeEInvoiceProvider", "Sequence"]
