# ruff: file-ignore[line-too-long]
"""Fake ``sms`` connector (dev / tests, AD-29): records every send in memory, returns deterministic ids, prints a
masked line when ``CONNECTOR_FAKE_ECHO=true`` and verifies webhooks with HMAC-SHA256 of the body
(``X-Fake-Signature``) using the configured webhook secret. Registered as provider ``fake`` outside production.

Webhook payload (JSON): ``{"events": [{"kind": "delivery"|"inbound", "provider_message_id", "status",
"from_e164", "to_e164", "text"}]}``.
"""

import hashlib
import hmac
import itertools
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from loguru import logger

from app.core.settings import get_settings
from app.services.connectors.registry import register_adapter
from app.services.connectors.sms.channel import ChannelAdapter, SendSpec, mask_phone
from app.services.connectors.sms.port import ChannelEvent, PermanentSendError, SendResult


def fake_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _echo(channel: str, spec: SendSpec) -> None:
    if os.environ.get("CONNECTOR_FAKE_ECHO", "").lower() in ("1", "true", "yes"):
        logger.info(
            "[fake {}] to={} template={} chars={}",
            channel,
            mask_phone(spec.to_e164),
            spec.template_ref,
            len(spec.body),
        )


def parse_fake_events(body: bytes) -> Mapping[str, Any]:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return {"events": []}
    raw = data.get("events") if isinstance(data, dict) else None
    events = []
    for item in raw or []:
        if isinstance(item, dict) and item.get("kind") in ("delivery", "inbound"):
            events.append(ChannelEvent.from_dict(item).as_dict())
    return {"events": events}


class FakeSmsProvider(ChannelAdapter):
    category = "sms"
    provider_key = "fake"
    calls: ClassVar[list[tuple[str, Any]]] = []
    fail_ops: ClassVar[set[str]] = set()
    fail_sends: ClassVar[int] = 0  # transient failures still to inject
    permanent_error: ClassVar[str | None] = None
    _ids = itertools.count(1)

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()
        cls.fail_ops.clear()
        cls.fail_sends = 0
        cls.permanent_error = None

    @classmethod
    def sends(cls) -> list[SendSpec]:
        return [args for op, args in cls.calls if op == "send"]

    async def _deliver(self, spec: SendSpec) -> SendResult:
        cls = type(self)
        if cls.permanent_error:
            raise PermanentSendError(cls.permanent_error)
        if cls.fail_sends > 0:
            cls.fail_sends -= 1
            msg = "fake sms provider unavailable"
            raise RuntimeError(msg)
        cls.calls.append(("send", spec))
        _echo("sms", spec)
        return SendResult(f"fake-sms-{next(cls._ids)}", "sent")

    async def send(
        self,
        to_e164: str,
        sender_id: str | None,
        dlt_template_id: str,
        dlt_entity_id: str | None,
        variables: Sequence[str],
        body: str,
    ) -> SendResult:
        return await self._deliver(
            SendSpec("sms", to_e164, body, dlt_template_id, list(variables), "en", sender_id, dlt_entity_id)
        )

    async def _other(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        type(self).calls.append((op, dict(payload)))
        if op in type(self).fail_ops:
            msg = f"fake {op} failure"
            raise RuntimeError(msg)
        return {"ok": True, "op": op}

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        secret = self.config.webhook_secret if self.config is not None else None
        if not secret:
            return False
        given = {k.lower(): v for k, v in headers.items()}.get("x-fake-signature", "")
        return hmac.compare_digest(given, fake_signature(secret, body))

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return parse_fake_events(body)


if get_settings().app_env != "production":
    register_adapter("sms", "fake", FakeSmsProvider)

__all__ = ["FakeSmsProvider", "fake_signature", "parse_fake_events"]
