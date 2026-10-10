# ruff: noqa: E501
"""Epic 76 (story 76.1, spec A2): the Exotel / Twilio / Plivo adapters against recorded fixtures (``httpx.MockTransport``
+ a stub resolver; no socket is opened): request mapping, status / price / duration parsing, webhook parsing, the
delete op, auth-error classification and the egress host allow-list. Plus the Conformance Kit per manifest."""

import base64
import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from app.core import net_safety
from app.core.hooks import autodiscover
from app.core.integrations import ConnectorAuthError, MappingSecretBag, make_context
from app.core.net_safety import EgressRefused
from app.services.connectors.voice.bridge import (
    OP_BRIDGE_CALL,
    OP_CALL_STATUS,
    OP_DELETE_RECORDING,
    BridgedCallRequest,
    billable_minutes,
    to_minor,
)
from app.services.connectors.voice.manifests import EXOTEL, PLIVO, TELEPHONY_MANIFESTS, TWILIO
from tests.connectors.conformance import run_conformance

autodiscover()

PUBLIC = "52.1.2.3"
HOSTS = ["api.twilio.com", "api.plivo.com", "media.plivo.com", "api.in.exotel.com", "api.exotel.com", "recordings.exotel.com"]


async def _resolve(host: str) -> list[str]:
    if host in HOSTS:
        return [PUBLIC]
    raise OSError("NXDOMAIN")


@pytest.fixture(autouse=True)
def _resolver() -> Iterator[None]:
    net_safety.set_resolver(_resolve)
    yield
    net_safety.set_resolver(None)


def _ctx(manifest: Any, secrets: dict[str, str], handler: Callable[[httpx.Request], httpx.Response], settings: dict[str, Any] | None = None) -> Any:
    ctx = make_context(
        manifest, tenant_id=uuid.uuid4(), installation_id=uuid.uuid4(), config_id=uuid.uuid4(), settings=settings or {},
        secrets=MappingSecretBag(secrets), mode="live", transport=httpx.MockTransport(handler),
    )  # fmt: skip
    return manifest.adapters["live"](ctx)


def _req(record: bool = True) -> BridgedCallRequest:
    return BridgedCallRequest(
        voice_call_id=uuid.uuid4(), agent_e164="+919800000001", candidate_e164="+919811110001", caller_id_e164="+918040000000",
        record=record, dual_channel=record, notice_text="This call is recorded." if record else None,
        status_callback_url="https://hooks.example.com/api/v1/hooks/voice/cfg", answer_url="https://hooks.example.com/api/v1/hooks/voice/cfg/answer/tok",
        max_duration_s=1800,
    )  # fmt: skip


@pytest.mark.parametrize("key", [m.key for m in TELEPHONY_MANIFESTS])
async def test_manifest_conforms(key: str) -> None:
    m = {x.key: x for x in TELEPHONY_MANIFESTS}[key]
    assert await run_conformance(m) == []


async def test_twilio_bridge_status_delete_and_parse() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Basic " + base64.b64encode(b"AC123:tok").decode()
        path = request.url.path
        if request.method == "POST" and path.endswith("/Calls.json"):
            return httpx.Response(201, json={"sid": "CA111", "status": "queued"})
        if request.method == "GET" and path.endswith("/Calls/CA111.json"):
            return httpx.Response(200, json={"sid": "CA111", "status": "completed", "duration": "75", "price": "-0.0130", "price_unit": "USD", "start_time": "Thu, 08 Oct 2026 10:00:00 +0000", "end_time": "Thu, 08 Oct 2026 10:01:15 +0000"})
        if request.method == "DELETE" and path.endswith("/Recordings/RE1.json"):
            return httpx.Response(204)
        if path.endswith("/Recordings/RE1.wav"):
            return httpx.Response(200, content=b"RIFF....", headers={"content-type": "audio/x-wav"})
        return httpx.Response(404)

    adapter = _ctx(TWILIO, {"account_sid": "AC123", "auth_token": "tok"}, handler)
    out = await adapter.handle(OP_BRIDGE_CALL, _req().to_payload())
    assert out == {"external_ref": "CA111", "status": "dialing_recruiter"}
    form = parse_qs(seen[0].content.decode())
    assert form["To"] == ["+919800000001"] and form["From"] == ["+918040000000"]
    assert form["Url"] == ["https://hooks.example.com/api/v1/hooks/voice/cfg/answer/tok"]
    assert form["StatusCallbackEvent"] == ["initiated", "ringing", "answered", "completed"]
    assert "+919811110001" not in seen[0].content.decode()  # the candidate is dialled from the TwiML only
    status = await adapter.handle(OP_CALL_STATUS, {"external_ref": "CA111"})
    assert (status["status"], status["duration_s"], status["price_minor"], status["currency"]) == ("completed", 75, 1, "USD")
    assert await adapter.handle(OP_DELETE_RECORDING, {"recording_ref": "RE1"}) == {"status": "deleted"}
    rec = await adapter.fetch_recording("RE1")
    assert rec is not None and rec.media_type == "audio/x-wav"
    # parse: parent leg, child leg, recording
    body = b"CallSid=CA111&CallStatus=ringing&SequenceNumber=1"
    assert adapter.parse_webhook({}, body)["kind"] == "ringing"
    child = adapter.parse_webhook({}, b"CallSid=CA222&ParentCallSid=CA111&CallStatus=in-progress&SequenceNumber=0")
    assert (child["external_ref"], child["kind"], child["leg"]) == ("CA111", "candidate_answered", "candidate")
    done = adapter.parse_webhook({}, b"CallSid=CA111&CallStatus=completed&CallDuration=75&SequenceNumber=3")
    assert (done["kind"], done["duration_s"]) == ("completed", 75)
    rec_ev = adapter.parse_webhook({}, b"CallSid=CA111&RecordingSid=RE1&RecordingStatus=completed&RecordingChannels=2")
    assert (rec_ev["kind"], rec_ev["recording_ref"], rec_ev["recording_channels"]) == ("recording_ready", "RE1", 2)


async def test_twilio_auth_error_is_classified_without_secrets() -> None:
    adapter = _ctx(TWILIO, {"account_sid": "AC123", "auth_token": "s3cret-token"}, lambda r: httpx.Response(401, json={"code": 20003}))
    with pytest.raises(ConnectorAuthError) as exc:
        await adapter.handle(OP_BRIDGE_CALL, _req().to_payload())
    assert "s3cret-token" not in str(exc.value)


async def test_plivo_bridge_and_parse() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.path == "/v1/Account/MA1/Call/":
            return httpx.Response(201, json={"request_uuid": "req-1", "message": "call fired"})
        if request.method == "DELETE" and request.url.path == "/v1/Account/MA1/Recording/rec-9/":
            return httpx.Response(204)
        return httpx.Response(404)

    adapter = _ctx(PLIVO, {"auth_id": "MA1", "auth_token": "tok"}, handler)
    out = await adapter.handle(OP_BRIDGE_CALL, _req().to_payload())
    assert out == {"external_ref": "req-1", "status": "dialing_recruiter"}
    sent = json.loads(seen[0].content)
    assert (sent["from"], sent["to"], sent["time_limit"]) == ("918040000000", "919800000001", 1800)
    assert await adapter.handle(OP_DELETE_RECORDING, {"recording_ref": "rec-9"}) == {"status": "deleted"}
    hang = adapter.parse_webhook({}, b"RequestUUID=req-1&CallUUID=c1&Event=Hangup&CallStatus=completed&BillDuration=61&TotalCost=0.0170")
    assert (hang["kind"], hang["duration_s"], hang["price_minor"]) == ("completed", 61, 2)
    b_leg = adapter.parse_webhook({}, b"RequestUUID=req-1&DialBLegStatus=answer")
    assert b_leg["kind"] == "candidate_answered"
    rec = adapter.parse_webhook({}, b"RequestUUID=req-1&RecordingID=rec-9&RecordingDuration=60")
    assert (rec["kind"], rec["recording_ref"]) == ("recording_ready", "rec-9")


async def test_exotel_bridge_records_only_with_flow_and_delete_unsupported() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/Calls/connect.json"):
            return httpx.Response(200, json={"Call": {"Sid": "ex-1", "Status": "in-progress"}})
        if request.url.path.endswith("/Calls/ex-1.json"):
            return httpx.Response(200, json={"Call": {"Sid": "ex-1", "Status": "completed", "Duration": "90", "Price": "1.20", "RecordingUrl": "https://recordings.exotel.com/a/ex-1.mp3"}})
        return httpx.Response(404)

    cfg = {"account_sid": "acme1", "subdomain": "api.in.exotel.com", "callback_ip_cidrs": ["10.0.0.0/8"]}
    no_flow = _ctx(EXOTEL, {"api_key": "k", "api_token": "t", "webhook_path_token": "p"}, handler, cfg)
    await no_flow.handle(OP_BRIDGE_CALL, _req(record=True).to_payload())
    form = parse_qs(seen[-1].content.decode())
    assert form["Record"] == ["false"] and "RecordingChannels" not in form
    assert form["From"] == ["+919800000001"] and form["To"] == ["+919811110001"]
    flow = _ctx(EXOTEL, {"api_key": "k", "api_token": "t", "webhook_path_token": "p"}, handler, {**cfg, "recording_flow_app_id": "12345"})
    out = await flow.handle(OP_BRIDGE_CALL, _req(record=True).to_payload())
    assert out["external_ref"] == "ex-1"
    form = parse_qs(seen[-1].content.decode())
    assert form["Record"] == ["true"] and form["RecordingChannels"] == ["dual"]
    status = await flow.handle(OP_CALL_STATUS, {"external_ref": "ex-1"})
    assert (status["status"], status["duration_s"], status["price_minor"], status["currency"]) == ("completed", 90, 120, "INR")
    assert status["recording_ref"].startswith("https://recordings.exotel.com/")
    assert (await flow.handle(OP_DELETE_RECORDING, {"recording_ref": "x"}))["status"] == "unsupported"
    parsed = flow.parse_webhook({}, b"CallSid=ex-1&Status=completed")
    assert parsed["verify_by_fetch"] is True


async def test_egress_outside_manifest_hosts_is_refused() -> None:
    adapter = _ctx(EXOTEL, {"api_key": "k", "api_token": "t"}, lambda r: httpx.Response(200), {"account_sid": "a"})
    with pytest.raises(Exception) as exc:
        await adapter.fetch_recording("https://evil.example.com/rec.mp3")
    assert "evil" not in str(exc.value) or isinstance(exc.value, EgressRefused)


def test_minutes_and_price_helpers() -> None:
    assert [billable_minutes(x) for x in (None, 0, 1, 60, 61, 125)] == [0, 0, 1, 1, 2, 3]
    assert (to_minor("-0.0130"), to_minor(0.65), to_minor(None), to_minor("x")) == (1, 65, None, None)
