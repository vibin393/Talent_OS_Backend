# ruff: noqa: E501
"""Epic 33 A4: MSG91 and Meta WhatsApp Cloud adapters against recorded responses (no live calls in CI)."""

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator

import httpx
import pytest

from app.services.connectors.base import ConnectorConfig
from app.services.connectors.sms.channel import SendSpec
from app.services.connectors.sms.msg91 import Msg91SmsProvider
from app.services.connectors.sms.port import PermanentSendError, events_from_payload
from app.services.connectors.whatsapp.meta_cloud import MetaCloudWhatsAppProvider


def _config(category: str, provider: str, credentials: dict, settings: dict | None = None) -> ConnectorConfig:
    return ConnectorConfig(
        uuid.uuid4(), uuid.uuid4(), category, provider, None, settings or {}, credentials, "whsec-x"
    )


@pytest.fixture
def requests() -> Iterator[list[httpx.Request]]:
    seen: list[httpx.Request] = []
    yield seen
    Msg91SmsProvider.transport = None
    MetaCloudWhatsAppProvider.transport = None


async def test_msg91_send_and_permanent_error(requests: list[httpx.Request]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if body["recipients"][0]["mobiles"] == "910000000000":
            return httpx.Response(400, json={"type": "error", "message": "Invalid mobile number"})
        return httpx.Response(200, json={"type": "success", "message": "3567686b6f68373133333538"})

    Msg91SmsProvider.transport = httpx.MockTransport(handler)
    adapter = Msg91SmsProvider(_config("sms", "msg91", {"auth_key": "k-secret"}))
    spec = SendSpec(
        "sms", "+919876543210", "Hi Priya", "1207161234567890123", ["Priya"], "en", "TALOS1", "1201159"
    )
    result = await adapter._deliver(spec)
    assert result.provider_message_id == "3567686b6f68373133333538"
    sent = json.loads(requests[0].content)
    assert requests[0].headers["authkey"] == "k-secret" and requests[0].url.path == "/api/v5/flow/"
    assert (
        sent["recipients"] == [{"mobiles": "919876543210", "var1": "Priya"}]
        and sent["DLT_TE_ID"] == spec.template_ref
    )
    assert sent["sender"] == "TALOS1" and sent["PE_ID"] == "1201159"
    with pytest.raises(PermanentSendError):
        await adapter._deliver(SendSpec("sms", "+910000000000", "x", spec.template_ref, []))


def test_msg91_webhook_signature_and_events() -> None:
    adapter = Msg91SmsProvider(_config("sms", "msg91", {"auth_key": "k"}))
    body = json.dumps({"data": [{"requestId": "r1", "status": "1", "number": "919876543210"}]}).encode()
    assert not adapter.verify_webhook({"X-Talentos-Signature": "nope"}, body)
    sig = hmac.new(b"whsec-x", body, hashlib.sha256).hexdigest()
    assert adapter.verify_webhook({"X-Talentos-Signature": sig}, body)
    (event,) = events_from_payload(adapter.parse_webhook({}, body))
    assert event.kind == "delivery" and event.status == "delivered" and event.provider_message_id == "r1"
    inbound = adapter.parse_webhook({}, json.dumps({"sender": "9876543210", "message": "STOP"}).encode())
    (ev,) = events_from_payload(inbound)
    assert ev.kind == "inbound" and ev.from_e164 == "+919876543210" and ev.text == "STOP"


async def test_meta_cloud_send_templates_and_webhook(requests: list[httpx.Request]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/messages"):
            body = json.loads(request.content)
            if body["template"]["name"] == "missing":
                return httpx.Response(
                    400, json={"error": {"code": 132001, "message": "Template name does not exist"}}
                )
            return httpx.Response(200, json={"messages": [{"id": "wamid.HBg1"}]})
        return httpx.Response(200, json={"data": [
            {"name": "timesheet_reminder", "language": "en", "status": "APPROVED", "components": [{"type": "BODY", "text": "Hi {{1}}"}]},
        ]})  # fmt: skip

    MetaCloudWhatsAppProvider.transport = httpx.MockTransport(handler)
    cfg = _config(
        "whatsapp",
        "meta_cloud",
        {"access_token": "tok", "app_secret": "app-secret"},
        {"phone_number_id": "111", "waba_id": "222"},
    )
    adapter = MetaCloudWhatsAppProvider(cfg)
    result = await adapter.send_template("+919876543210", "timesheet_reminder", "en", ["Priya"])
    assert result.provider_message_id == "wamid.HBg1"
    sent = json.loads(requests[0].content)
    assert (
        requests[0].url.path == "/v20.0/111/messages" and requests[0].headers["Authorization"] == "Bearer tok"
    )
    assert sent["to"] == "919876543210" and sent["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "Priya"}
    ]
    with pytest.raises(PermanentSendError) as exc:
        await adapter.send_template("+919876543210", "missing", "en", [])
    assert exc.value.code == "meta_132001"
    (tpl,) = await adapter.list_templates()
    assert tpl.name == "timesheet_reminder" and tpl.status == "APPROVED" and tpl.variable_count == 1
    hook = json.dumps({"entry": [{"changes": [{"value": {
        "statuses": [{"id": "wamid.HBg1", "status": "read", "recipient_id": "919876543210"}],
        "messages": [{"from": "919876543210", "id": "wamid.in", "timestamp": "1790000000", "type": "text", "text": {"body": "Thanks"}}],
    }}]}]}).encode()  # fmt: skip
    good = "sha256=" + hmac.new(b"app-secret", hook, hashlib.sha256).hexdigest()
    assert adapter.verify_webhook({"X-Hub-Signature-256": good}, hook)
    assert not adapter.verify_webhook({"X-Hub-Signature-256": "sha256=00"}, hook)
    kinds = [(e.kind, e.status or e.text) for e in events_from_payload(adapter.parse_webhook({}, hook))]
    assert kinds == [("delivery", "read"), ("inbound", "Thanks")]
