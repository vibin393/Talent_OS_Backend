"""Fake ``assessment`` connector for tests (AD-29): canned tests and results keyed by ``external_test_id``;
records calls (``calls`` / ``fail_ops`` / ``reset()``) and verifies webhooks with HMAC-SHA256 of the body
(``X-Fake-Signature``). Registered as provider ``fake`` outside production."""

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

from app.core.settings import get_settings
from app.services.connectors.assessment.port import (
    ProviderInviteDTO,
    ProviderInviteResultDTO,
    ProviderResultDTO,
    ProviderTestDTO,
    ProviderWebhookDTO,
)
from app.services.connectors.base import ConnectorConfig
from app.services.connectors.registry import register_adapter

TESTS: tuple[ProviderTestDTO, ...] = (
    ProviderTestDTO("fake-python-101", "Python fundamentals", 45, ("python",)),
    ProviderTestDTO("fake-sql-201", "SQL intermediate", 30, ("sql", "postgresql")),
)
# Canned score per external test id (0..100); unknown tests score 50.
SCORES: dict[str, Decimal] = {"fake-python-101": Decimal("82.50"), "fake-sql-201": Decimal("64.00")}


def _invite_dto(payload: Mapping[str, Any]) -> ProviderInviteDTO:
    """Rebuild the frozen minimal invite DTO from a job payload (Epic 40 ``provider.invite_payload``)."""
    deadline = payload.get("deadline_at")
    return ProviderInviteDTO(
        tenant_id=uuid.UUID(str(payload["tenant_id"])),
        invitation_id=uuid.UUID(str(payload["invitation_id"])),
        external_test_id=str(payload.get("external_test_id") or ""),
        candidate_email=str(payload.get("candidate_email") or ""),
        candidate_name=str(payload.get("candidate_name") or ""),
        deadline_at=datetime.fromisoformat(deadline) if isinstance(deadline, str) and deadline else None,
        callback_url=payload.get("callback_url"),
    )


class FakeAssessmentProvider:
    category = "assessment"
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
        if op == "invite" and "invitation_id" in payload:  # Epic 40: connector job -> the port's ``invite``
            out = await self.invite(_invite_dto(payload))
            return {"ok": True, "op": op, "external_ref": out.external_ref, "take_url": out.take_url}
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

    async def list_tests(self) -> Sequence[ProviderTestDTO]:
        self._record("list_tests", ())
        return list(TESTS)

    async def invite(self, req: ProviderInviteDTO) -> ProviderInviteResultDTO:
        self._record("invite", (req,))
        return ProviderInviteResultDTO(
            external_ref=f"fake-asmt:{req.external_test_id}:{req.invitation_id}",
            take_url=f"https://assessments.fake.test/take/{req.invitation_id}",
        )

    async def fetch_result(self, external_ref: str) -> ProviderResultDTO | None:
        self._record("fetch_result", (external_ref,))
        parts = external_ref.split(":")
        if len(parts) != 3 or parts[0] != "fake-asmt":
            return None
        score = SCORES.get(parts[1], Decimal("50.00"))
        tags = next((t.skill_tags for t in TESTS if t.external_id == parts[1]), ())
        return ProviderResultDTO(
            status="completed",
            score=score,
            section_scores={"main": score},
            skill_scores=dict.fromkeys(tags, score),
            report_url=f"https://assessments.fake.test/report/{parts[2]}",
            completed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> ProviderWebhookDTO:
        self._record("handle_webhook", (headers, body))
        ref = str(self.parse_webhook(headers, body).get("external_ref", ""))
        return ProviderWebhookDTO(external_ref=ref, result=await self.fetch_result(ref) if ref else None)


if get_settings().app_env != "production":
    register_adapter("assessment", "fake", FakeAssessmentProvider)

__all__ = ["FakeAssessmentProvider", "Sequence"]
