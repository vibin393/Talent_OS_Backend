# ruff: file-ignore[line-too-long]
"""Fake ``bgv`` connector for tests (AD-29): records calls, returns deterministic references and reports, verifies
webhooks with HMAC-SHA256 of the body (``X-Fake-Signature``) using the configured webhook secret. Registered as
provider ``fake`` outside production."""

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from app.core.settings import get_settings
from app.services.connectors.base import ConnectorConfig
from app.services.connectors.bgv.port import BgvCheckJob, BgvReportDTO, BgvStatusDTO, BgvWebhookDTO
from app.services.connectors.registry import register_adapter


class FakeBgvProvider:
    category = "bgv"
    provider_key = "fake"
    key = "fake"
    calls: ClassVar[list[tuple[str, Any]]] = []
    fail_ops: ClassVar[set[str]] = set()
    reports: ClassVar[dict[str, BgvReportDTO]] = {}

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()
        cls.fail_ops.clear()
        cls.reports.clear()

    def _record(self, op: str, payload: Any) -> None:
        type(self).calls.append((op, payload))
        if op in type(self).fail_ops:
            msg = f"fake {op} failure"
            raise RuntimeError(msg)

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._record(op, dict(payload))
        if op == "initiate":
            return {"ok": True, "op": op, "external_ref": f"fake-{payload.get('check_id')}"}
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

    async def initiate_check(self, job: BgvCheckJob) -> str:
        self._record("initiate_check", job)
        return f"fake-{job.check_id}"

    async def get_status(self, external_ref: str) -> BgvStatusDTO:
        self._record("get_status", external_ref)
        return BgvStatusDTO(
            external_ref, "completed" if external_ref in type(self).reports else "in_progress"
        )

    async def fetch_report(self, external_ref: str) -> BgvReportDTO | None:
        self._record("fetch_report", external_ref)
        return type(self).reports.get(external_ref)

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> BgvWebhookDTO:
        data = self.parse_webhook(headers, body)
        ref = str(data.get("external_ref") or "")
        self._record("handle_webhook", ref)
        return BgvWebhookDTO(ref, type(self).reports.get(ref))


if get_settings().app_env != "production":
    register_adapter("bgv", "fake", FakeBgvProvider)

__all__ = ["FakeBgvProvider", "Sequence"]
