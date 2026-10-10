"""Fake ``accounting`` connector for tests (AD-29): records calls, returns deterministic results, verifies
webhooks with HMAC-SHA256 of the body (``X-Fake-Signature``) using the configured webhook secret.
Registered as provider ``fake`` outside production."""

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, ClassVar

from app.core.settings import get_settings
from app.services.connectors.accounting.port import LedgerDocumentDTO, LedgerPaymentDTO, LedgerResultDTO
from app.services.connectors.base import ConnectorConfig
from app.services.connectors.registry import register_adapter


class FakeAccountingExporter:
    category = "accounting"
    provider_key = "fake"
    key = "fake"
    calls: ClassVar[list[tuple[str, Any]]] = []
    fail_ops: ClassVar[set[str]] = set()
    # Epic 66 (AccountingSyncPort): document refs by (document_id, version) and scripted payments.
    ledger: ClassVar[dict[tuple[uuid.UUID, int], str]] = {}
    payments: ClassVar[list[LedgerPaymentDTO]] = []

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()
        cls.fail_ops.clear()
        cls.ledger.clear()
        cls.payments.clear()

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

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # noqa: ARG002
        try:
            data = json.loads(body or b'{}')
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"data": data}

    async def export(self, rows: Sequence[Mapping[str, Any]], template: Mapping[str, Any]) -> bytes:
        type(self).calls.append(("export", (rows, template)))
        return b"fake"

    # ---------------------------------------------------------------- AccountingSyncPort (Epic 66)
    async def push_documents(self, docs: Sequence[LedgerDocumentDTO]) -> Sequence[LedgerResultDTO]:
        type(self).calls.append(("push_documents", tuple(d.document_id for d in docs)))
        if "push_documents" in type(self).fail_ops:
            msg = "fake push_documents failure"
            raise RuntimeError(msg)
        out: list[LedgerResultDTO] = []
        for doc in docs:
            key = (doc.document_id, doc.document_version)
            ref = type(self).ledger.get(key)
            if ref is None:
                ref = f"FAKE-LEDGER-{len(type(self).ledger) + 1}"
                type(self).ledger[key] = ref
            out.append(LedgerResultDTO(document_id=doc.document_id, external_ref=ref, status="synced", message=None))
        return out

    async def document_status(self, refs: Sequence[str]) -> Sequence[LedgerResultDTO]:
        type(self).calls.append(("document_status", tuple(refs)))
        by_ref = {ref: key for key, ref in type(self).ledger.items()}
        return [
            LedgerResultDTO(
                document_id=by_ref[ref][0] if ref in by_ref else uuid.UUID(int=0),
                external_ref=ref,
                status="synced" if ref in by_ref else "failed",
                message=None if ref in by_ref else "unknown reference",
            )
            for ref in refs
        ]

    async def pull_payments(
        self, since: datetime | None, cursor: str | None
    ) -> tuple[Sequence[LedgerPaymentDTO], str | None]:
        type(self).calls.append(("pull_payments", (since, cursor)))
        start = int(cursor) if cursor and cursor.isdigit() else 0
        items = list(type(self).payments)[start : start + 100]
        nxt = str(start + 100) if start + 100 < len(type(self).payments) else None
        return items, nxt

    async def masters_sync(
        self, customers: Sequence[Mapping[str, Any]], ledgers: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, str]:
        type(self).calls.append(("masters_sync", (len(customers), len(ledgers))))
        out: dict[str, str] = {}
        for item in [*customers, *ledgers]:
            name = str(item.get("name") or item.get("id") or "")
            if name:
                out[name] = f"fake-master-{hashlib.sha256(name.encode()).hexdigest()[:10]}"
        return out


if get_settings().app_env != "production":
    register_adapter("accounting", "fake", FakeAccountingExporter)

__all__ = ["FakeAccountingExporter", "Sequence"]
