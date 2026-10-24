"""Fake ``esign`` connector for tests (AD-29): records calls, returns deterministic results, verifies
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


class FakeEsignProvider:
    category = "esign"
    provider_key = "fake"
    key = "fake"
    calls: ClassVar[list[tuple[str, Any]]] = []
    fail_ops: ClassVar[set[str]] = set()
    # Epic 22: scripted provider state per external ref (tests set ``statuses[ref] = "completed"``).
    statuses: ClassVar[dict[str, str]] = {}
    pdfs: ClassVar[dict[str, bytes]] = {}

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()
        cls.fail_ops.clear()
        cls.statuses.clear()
        cls.pdfs.clear()

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # NOSONAR
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
            data = json.loads(body or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"data": data}

    async def create_request(self, req: Any) -> str:  # NOSONAR
        type(self).calls.append(("create_request", (req,)))
        if "create_request" in type(self).fail_ops:
            msg = "fake create_request failure"
            raise RuntimeError(msg)
        ref = f"fake-esign-{getattr(req, 'request_id', 'x')}"
        type(self).statuses.setdefault(ref, "sent")
        type(self).pdfs[ref] = getattr(req, "pdf", b"")
        return ref

    async def get_status(self, external_ref: str) -> Any:  # NOSONAR
        from app.services.connectors.esign.port import EsignStatusDTO

        type(self).calls.append(("get_status", (external_ref,)))
        return EsignStatusDTO(status=type(self).statuses.get(external_ref, "sent"))

    async def fetch_signed(self, external_ref: str) -> bytes | None:  # NOSONAR
        type(self).calls.append(("fetch_signed", (external_ref,)))
        return type(self).pdfs.get(external_ref)

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> Any:  # NOSONAR
        from app.services.connectors.esign.port import EsignStatusDTO, EsignWebhookDTO

        type(self).calls.append(("handle_webhook", (headers, body)))
        data = self.parse_webhook(headers, body)
        return EsignWebhookDTO(
            str(data.get("external_ref", "")), EsignStatusDTO(status=str(data.get("status", "sent")))
        )


if get_settings().app_env != "production":
    register_adapter("esign", "fake", FakeEsignProvider)

__all__ = ["FakeEsignProvider", "Sequence"]
