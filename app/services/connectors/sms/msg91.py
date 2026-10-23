# ruff: file-ignore[line-too-long]
"""MSG91 ``sms`` adapter (Flow API with India DLT fields; Epic 33 A4). The only file that knows MSG91 specifics.

Config (``provider_connector_configs``): credentials ``{"auth_key"}``; settings ``{"flow_id"?, "base_url"?}``;
``webhook_secret`` signs delivery / inbound callbacks.

* Send: ``POST {base}/api/v5/flow/`` with header ``authkey`` and ``{"template_id": <flow id or DLT template id>,
  "sender", "DLT_TE_ID", "short_url": "0", "recipients": [{"mobiles": "<digits>", "var1": ..}]}``. A
  ``type=success`` answer carries the request id. 4xx answers naming an invalid number / template are permanent
  (no retry); 5xx and network errors are retried by ``connector_jobs``.
* Webhooks: ``X-Talentos-Signature: <hex HMAC-SHA256(webhook_secret, body)>`` (configure MSG91 to call
  ``/api/v1/hooks/sms/<config_id>?…`` through a signing relay); delivery reports ``[{"requestId", "status",
  "number"}]`` or ``{"data": [...]}``; inbound ``{"sender", "message", "receivedAt"?}``.

Tests use recorded fixtures through ``transport`` (``httpx.MockTransport``); CI never calls MSG91.
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

BASE_URL = "https://control.msg91.com"
TIMEOUT_SECONDS = 10.0
PERMANENT_HINTS = ("invalid mobile", "invalid number", "template", "dlt", "blocked", "dnd")
STATUS_MAP = {"1": "delivered", "2": "failed", "9": "failed", "16": "failed", "17": "failed", "25": "failed", "26": "failed",
              "delivered": "delivered", "failed": "failed", "rejected": "failed", "sent": "sent", "submitted": "sent"}  # fmt: skip


def _digits(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def _e164(number: str | None) -> str | None:
    if not number:
        return None
    digits = _digits(str(number))
    if len(digits) == 10:  # Indian mobile without country code
        digits = f"91{digits}"
    return f"+{digits}" if digits else None


class Msg91SmsProvider(ChannelAdapter):
    category = "sms"
    provider_key = "msg91"
    transport: ClassVar[httpx.AsyncBaseTransport | None] = None  # tests: recorded fixtures

    def _client(self) -> httpx.AsyncClient:
        settings = dict(self.config.settings) if self.config is not None else {}
        return httpx.AsyncClient(
            base_url=str(settings.get("base_url") or BASE_URL),
            timeout=TIMEOUT_SECONDS,
            transport=type(self).transport,
        )

    def _auth_key(self) -> str:
        creds = dict(self.config.credentials or {}) if self.config is not None else {}
        key = str(creds.get("auth_key") or "")
        if not key:
            raise PermanentSendError("connector_credentials_missing")
        return key

    async def _deliver(self, spec: SendSpec) -> SendResult:
        settings = dict(self.config.settings) if self.config is not None else {}
        recipient: dict[str, str] = {"mobiles": _digits(spec.to_e164)}
        for i, value in enumerate(spec.variables, start=1):
            recipient[f"var{i}"] = str(value)
        body: dict[str, Any] = {
            "template_id": str(settings.get("flow_id") or spec.template_ref),
            "short_url": "0",
            "recipients": [recipient],
            "DLT_TE_ID": spec.template_ref,
        }
        if spec.sender_id:
            body["sender"] = spec.sender_id
        if spec.dlt_entity_id:
            body["PE_ID"] = spec.dlt_entity_id
        async with self._client() as client:
            resp = await client.post(
                "/api/v5/flow/",
                json=body,
                headers={"authkey": self._auth_key(), "accept": "application/json"},
            )
        text = resp.text.lower()
        if 400 <= resp.status_code < 500:
            if resp.status_code in (401, 403):
                raise PermanentSendError("provider_auth_failed")
            if any(h in text for h in PERMANENT_HINTS):
                raise PermanentSendError("provider_rejected")
            msg = f"msg91 send: {resp.status_code}"
            raise RuntimeError(msg)
        if resp.status_code >= 500:
            msg = f"msg91 send: {resp.status_code}"
            raise RuntimeError(msg)
        try:
            data = resp.json()
        except ValueError as exc:
            msg = "msg91 send: invalid response"
            raise RuntimeError(msg) from exc
        if str(data.get("type", "")).lower() != "success":
            if any(h in str(data.get("message", "")).lower() for h in PERMANENT_HINTS):
                raise PermanentSendError("provider_rejected")
            msg = "msg91 send: not accepted"
            raise RuntimeError(msg)
        return SendResult(str(data.get("message") or data.get("request_id") or "") or None, "sent")

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

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        secret = self.config.webhook_secret if self.config is not None else None
        if not secret:
            return False
        given = {k.lower(): v for k, v in headers.items()}.get("x-talentos-signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(given, expected)

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return {"events": []}
        events: list[ChannelEvent] = []
        if isinstance(data, dict) and data.get("sender") and data.get("message") is not None:
            events.append(
                ChannelEvent(
                    "inbound",
                    provider_message_id=str(data.get("id") or data.get("requestId") or "") or None,
                    from_e164=_e164(data.get("sender")),
                    text=str(data.get("message"))[:2000],
                    received_at=datetime.now(UTC),
                )
            )
        reports = data.get("data") if isinstance(data, dict) else data
        for item in reports if isinstance(reports, list) else []:
            if not isinstance(item, dict):
                continue
            status = STATUS_MAP.get(str(item.get("status", "")).lower())
            if item.get("requestId") and status:
                events.append(
                    ChannelEvent(
                        "delivery",
                        provider_message_id=str(item["requestId"]),
                        status=status,
                        error_code=str(item.get("desc") or "")[:64] or None if status == "failed" else None,
                        to_e164=_e164(item.get("number")),
                    )
                )
        return {"events": [e.as_dict() for e in events]}


register_adapter("sms", "msg91", Msg91SmsProvider)

__all__ = ["Msg91SmsProvider"]
