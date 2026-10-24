"""Fake ``fx`` connector for tests (AD-29): deterministic 6 dp rates per pair and date
(``source="feed:fake"``); records calls (``calls`` / ``fail_ops`` / ``reset()``); HMAC ``X-Fake-Signature``
webhooks. Registered as ``fake`` outside production."""

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, ClassVar

from app.core.settings import get_settings
from app.services.connectors.base import ConnectorConfig
from app.services.connectors.fx.port import FxRateDTO
from app.services.connectors.registry import register_adapter

SOURCE = "feed:fake"
_SIX = Decimal("0.000001")


def fake_rate(from_ccy: str, to_ccy: str, on: date) -> Decimal:
    """Deterministic rate; the reverse pair is its reciprocal and the same currency is 1."""
    if from_ccy == to_ccy:
        return Decimal("1.000000")
    a, b = sorted((from_ccy, to_ccy))
    seed = int(hashlib.sha256(f"{a}:{b}:{on.isoformat()}".encode()).hexdigest()[:8], 16)
    base = Decimal(seed % 100000) / Decimal(1000) + Decimal("0.5")
    rate = base if (from_ccy, to_ccy) == (a, b) else Decimal(1) / base
    return rate.quantize(_SIX, rounding=ROUND_HALF_UP)


class FakeFxRateSource:
    category = "fx"
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

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # NOSONAR
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

    async def fetch_rates(self, on: date, pairs: Sequence[tuple[str, str]]) -> Sequence[FxRateDTO]:  # NOSONAR
        self._record("fetch_rates", (on, tuple(pairs)))
        return [
            FxRateDTO(from_ccy=f, to_ccy=t, rate=fake_rate(f, t, on), rate_date=on, source=SOURCE)
            for f, t in pairs
        ]


if get_settings().app_env != "production":
    register_adapter("fx", "fake", FakeFxRateSource)

__all__ = ["SOURCE", "FakeFxRateSource", "fake_rate"]
