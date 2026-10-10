"""Fake ``whatsapp`` connector (dev / tests, AD-29): records template sends in memory, serves a configurable
template list, verifies webhooks with HMAC-SHA256 (``X-Fake-Signature``). Registered as ``fake`` outside
production. Webhook payload: ``{"events": [...]}`` (see ``connectors.sms.fake``)."""

import hmac
import itertools
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from app.core.settings import get_settings
from app.services.connectors.registry import register_adapter
from app.services.connectors.sms.channel import ChannelAdapter, SendSpec
from app.services.connectors.sms.fake import _echo, fake_signature, parse_fake_events
from app.services.connectors.sms.port import PermanentSendError, SendResult
from app.services.connectors.whatsapp.port import ProviderTemplate


class FakeWhatsAppProvider(ChannelAdapter):
    category = "whatsapp"
    provider_key = "fake"
    calls: ClassVar[list[tuple[str, Any]]] = []
    fail_ops: ClassVar[set[str]] = set()
    fail_sends: ClassVar[int] = 0
    permanent_error: ClassVar[str | None] = None
    templates: ClassVar[list[ProviderTemplate]] = []
    _ids = itertools.count(1)

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()
        cls.fail_ops.clear()
        cls.fail_sends = 0
        cls.permanent_error = None
        cls.templates = []

    @classmethod
    def sends(cls) -> list[SendSpec]:
        return [args for op, args in cls.calls if op == "send_template"]

    async def _deliver(self, spec: SendSpec) -> SendResult:
        cls = type(self)
        if cls.permanent_error:
            raise PermanentSendError(cls.permanent_error)
        if cls.fail_sends > 0:
            cls.fail_sends -= 1
            msg = "fake whatsapp provider unavailable"
            raise RuntimeError(msg)
        cls.calls.append(("send_template", spec))
        _echo("whatsapp", spec)
        return SendResult(f"wamid.fake-{next(cls._ids)}", "sent")

    async def send_template(
        self, to_e164: str, template_name: str, language: str, variables: Sequence[str]
    ) -> SendResult:
        return await self._deliver(
            SendSpec("whatsapp", to_e164, "", template_name, list(variables), language)
        )

    async def list_templates(self) -> list[ProviderTemplate]:
        type(self).calls.append(("list_templates", ()))
        return list(type(self).templates)

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

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # noqa: ARG002
        return parse_fake_events(body)


if get_settings().app_env != "production":
    register_adapter("whatsapp", "fake", FakeWhatsAppProvider)

__all__ = ["FakeWhatsAppProvider"]
