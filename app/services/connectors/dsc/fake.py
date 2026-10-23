"""Fake ``dsc`` connector for tests (AD-29): ``sign_pdf`` returns the input bytes with a marker trailer
(``status="signed"``); records calls (``calls`` / ``fail_ops`` / ``reset()``); HMAC ``X-Fake-Signature``
webhooks. Registered as provider ``fake`` outside production."""

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from app.core.settings import get_settings
from app.services.connectors.base import ConnectorConfig
from app.services.connectors.dsc.port import DscSignRequestDTO, DscSignResultDTO
from app.services.connectors.registry import register_adapter

MARKER = b"\n%FAKE-DSC-SIGNED\n"


class FakeDscSigner:
    category = "dsc"
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

    def _record(self, op: str, args: Any) -> None:
        type(self).calls.append((op, args))
        if op in type(self).fail_ops:
            msg = f"fake {op} failure"
            raise RuntimeError(msg)

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._record(op, dict(payload))
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

    async def sign_pdf(self, req: DscSignRequestDTO) -> DscSignResultDTO:
        self._record("sign_pdf", (req,))
        return DscSignResultDTO(
            status="signed", signed_pdf=req.pdf + MARKER, external_ref=f"fake-dsc-{req.sha256[:12]}"
        )


if get_settings().app_env != "production":
    register_adapter("dsc", "fake", FakeDscSigner)

__all__ = ["MARKER", "FakeDscSigner", "Sequence"]
