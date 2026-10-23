"""Meta WhatsApp Cloud API adapter (Epic 33 A4). The only file that knows Meta specifics.

Config: credentials ``{"access_token", "app_secret"}``; settings ``{"phone_number_id", "waba_id",
"api_version"?}``. ``X-Hub-Signature-256: sha256=<HMAC-SHA256(app_secret, body)>`` verifies webhooks (the
config's ``webhook_secret`` is used when ``app_secret`` is absent).

* ``send_template``: ``POST /{phone_number_id}/messages`` (``type=template``, body parameters in order).
  Error codes for invalid recipients / missing or paused templates are permanent; 5xx / rate limits retry.
* ``list_templates``: ``GET /{waba_id}/message_templates``.
* Webhooks: ``entry[].changes[].value.statuses[]`` (delivery receipts) and ``.messages[]`` (inbound text).

Tests use recorded fixtures through ``transport``; CI never calls Meta.
"""

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx

from app.services.connectors.registry import register_adapter
from app.services.connectors.sms.channel import ChannelAdapter, SendSpec
from app.services.connectors.sms.port import ChannelEvent, PermanentSendError, SendResult
from app.services.connectors.whatsapp.port import ProviderTemplate

GRAPH = "https://graph.facebook.com"
DEFAULT_VERSION = "v20.0"
TIMEOUT_SECONDS = 10.0
# Recipient not on WhatsApp / invalid, template missing / paused / parameter mismatch, re-engagement window.
PERMANENT_CODES = frozenset(
    {131026, 131030, 131008, 131009, 132000, 132001, 132005, 132007, 132012, 132015, 132016, 131047}
)
STATUS_MAP = {"sent": "sent", "delivered": "delivered", "read": "read", "failed": "failed"}


def _e164(wa_id: str | None) -> str | None:
    digits = "".join(ch for ch in str(wa_id or "") if ch.isdigit())
    return f"+{digits}" if digits else None


class MetaCloudWhatsAppProvider(ChannelAdapter):
    category = "whatsapp"
    provider_key = "meta_cloud"
    transport: ClassVar[httpx.AsyncBaseTransport | None] = None  # tests: recorded fixtures

    def _settings(self) -> dict[str, Any]:
        return dict(self.config.settings) if self.config is not None else {}

    def _creds(self) -> dict[str, Any]:
        return dict(self.config.credentials or {}) if self.config is not None else {}

    def _client(self) -> httpx.AsyncClient:
        version = str(self._settings().get("api_version") or DEFAULT_VERSION)
        token = str(self._creds().get("access_token") or "")
        if not token:
            raise PermanentSendError("connector_credentials_missing")
        return httpx.AsyncClient(
            base_url=f"{GRAPH}/{version}",
            timeout=TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {token}"},
            transport=type(self).transport,
        )

    @staticmethod
    def _raise_for(resp: httpx.Response, what: str) -> dict[str, Any]:
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            code = int(((data.get("error") or {}).get("code")) or 0) if isinstance(data, dict) else 0
            if code in PERMANENT_CODES or resp.status_code in (401, 403):
                raise PermanentSendError(f"meta_{code or resp.status_code}")
            msg = f"meta {what}: {resp.status_code}"
            raise RuntimeError(msg)
        return data if isinstance(data, dict) else {}

    async def _deliver(self, spec: SendSpec) -> SendResult:
        phone_number_id = str(self._settings().get("phone_number_id") or "")
        if not phone_number_id:
            raise PermanentSendError("connector_settings_missing")
        template: dict[str, Any] = {"name": spec.template_ref, "language": {"code": spec.language or "en"}}
        if spec.variables:
            template["components"] = [
                {"type": "body", "parameters": [{"type": "text", "text": str(v)} for v in spec.variables]}
            ]
        body = {
            "messaging_product": "whatsapp",
            "to": spec.to_e164.lstrip("+"),
            "type": "template",
            "template": template,
        }
        async with self._client() as client:
            data = self._raise_for(await client.post(f"/{phone_number_id}/messages", json=body), "send")
        messages = data.get("messages") or []
        return SendResult(str(messages[0].get("id")) if messages else None, "sent")

    async def send_template(
        self, to_e164: str, template_name: str, language: str, variables: Sequence[str]
    ) -> SendResult:
        return await self._deliver(
            SendSpec("whatsapp", to_e164, "", template_name, list(variables), language)
        )

    async def list_templates(self) -> list[ProviderTemplate]:
        waba_id = str(self._settings().get("waba_id") or "")
        if not waba_id:
            raise PermanentSendError("connector_settings_missing")
        async with self._client() as client:
            data = self._raise_for(
                await client.get(
                    f"/{waba_id}/message_templates",
                    params={"fields": "name,language,status,components", "limit": "200"},
                ),
                "templates",
            )
        out: list[ProviderTemplate] = []
        for item in data.get("data") or []:
            body = next(
                (c.get("text", "") for c in item.get("components") or [] if c.get("type") == "BODY"), ""
            )
            out.append(
                ProviderTemplate(
                    name=str(item.get("name")),
                    language=str(item.get("language") or "en"),
                    status=str(item.get("status") or ""),
                    body=str(body),
                    variable_count=str(body).count("{{"),
                )
            )
        return out

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        secret = str(self._creds().get("app_secret") or "") or (
            self.config.webhook_secret if self.config else None
        )
        if not secret:
            return False
        given = {k.lower(): v for k, v in headers.items()}.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(given, expected)

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return {"events": []}
        events: list[ChannelEvent] = []
        for entry in data.get("entry") or [] if isinstance(data, dict) else []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for st in value.get("statuses") or []:
                    status = STATUS_MAP.get(str(st.get("status")))
                    if status and st.get("id"):
                        errors = st.get("errors") or []
                        events.append(
                            ChannelEvent(
                                "delivery",
                                provider_message_id=str(st["id"]),
                                status=status,
                                error_code=f"meta_{errors[0].get('code')}" if errors else None,
                                to_e164=_e164(st.get("recipient_id")),
                            )
                        )
                for msg in value.get("messages") or []:
                    text = (msg.get("text") or {}).get("body") or (msg.get("button") or {}).get("text") or ""
                    ts = msg.get("timestamp")
                    events.append(
                        ChannelEvent(
                            "inbound",
                            provider_message_id=str(msg.get("id") or "") or None,
                            from_e164=_e164(msg.get("from")),
                            text=str(text)[:2000],
                            received_at=datetime.fromtimestamp(int(ts), UTC) if ts else datetime.now(UTC),
                        )
                    )
        return {"events": [e.as_dict() for e in events]}


register_adapter("whatsapp", "meta_cloud", MetaCloudWhatsAppProvider)

__all__ = ["MetaCloudWhatsAppProvider"]
