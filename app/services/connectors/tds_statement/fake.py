"""Fake ``tds_statement`` connector for tests (AD-29): deterministic lines per TAN / FY / quarter; records
calls (``calls`` / ``fail_ops`` / ``reset()``); HMAC ``X-Fake-Signature`` webhooks. Registered as ``fake``
outside production."""

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar

from app.core.settings import get_settings
from app.services.connectors.base import ConnectorConfig
from app.services.connectors.registry import register_adapter
from app.services.connectors.tds_statement.port import Quarter, TdsStatementDTO, TdsStatementLineDTO

_QUARTER_MONTH = {"Q1": 4, "Q2": 7, "Q3": 10, "Q4": 1}


class FakeTdsStatementSource:
    category = "tds_statement"
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

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # noqa: ARG002
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"data": data}

    async def fetch(self, tan: str, fy: str, quarter: Quarter) -> TdsStatementDTO:
        self._record("fetch", (tan, fy, quarter))
        seed = int(hashlib.sha256(f"{tan}:{fy}:{quarter}".encode()).hexdigest()[:8], 16)
        start_year = int(fy[:4]) if fy[:4].isdigit() else 2026
        year = start_year + 1 if quarter == "Q4" else start_year
        lines = []
        for i in range(1 + seed % 3):
            paid = Decimal(100000 + (seed >> (i * 4)) % 900000).quantize(Decimal("0.01"))
            lines.append(
                TdsStatementLineDTO(
                    line_ref=f"{tan}-{fy}-{quarter}-{i + 1}",
                    deductor_tan=f"FAKE{(seed + i) % 100000:05d}X",
                    deductor_name=f"Fake Deductor {i + 1}",
                    section="194J",
                    transaction_date=date(year, _QUARTER_MONTH[quarter], 10 + i),
                    amount_paid=paid,
                    tds_deducted=(paid * Decimal("0.10")).quantize(Decimal("0.01")),
                    booking_status="F",
                )
            )
        return TdsStatementDTO(tan=tan, fy=fy, quarter=quarter, lines=tuple(lines))


if get_settings().app_env != "production":
    register_adapter("tds_statement", "fake", FakeTdsStatementSource)

__all__ = ["FakeTdsStatementSource", "Sequence"]
