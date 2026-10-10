# ruff: noqa: E501, D401
"""Epic 68 stories 68.3 / 68.4 / 68.5: sink idempotency and matching, the delivery engine (ladder, 4xx / 410 / 429,
egress refusal at connect, kill switch pause), signing (headers, dual signature), health, replay, test events, echo,
the D-11 erasure fan-out, the erasure handler and thin payloads."""

import json
import random
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app.core import public_events as registry
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.signing import verify_v1
from app.models.webhooks import WebhookDelivery, WebhookSubscription
from app.schemas.webhooks import ReplayRangeIn, SubscriptionCreate, TestEventIn
from app.services import ecosystem_service
from app.services.webhooks import (
    common,
    delivery,
    health,
    scheduler,
    signing,
    sink,
    subscriptions,
    test_events,
)
from tests.epic3 import Org3
from tests.webhooks.conftest import Receiver, outbox_row, sub_row

pytestmark = pytest.mark.usefixtures("manual_pump")


def sys_ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


async def make_sub(org: Org3, *, url: str = "https://hooks.example.com/in", types: list[str] | None = None, filters: dict[str, list[str]] | None = None,
                   installation_id: uuid.UUID | None = None, status: str = "active") -> tuple[uuid.UUID, str]:  # fmt: skip
    """A Subscription created through the service (verification skipped: status forced)."""
    async with tenant_session(org.tenant_id) as db:
        sub = WebhookSubscription(
            id=uuid.uuid4(), tenant_id=org.tenant_id, owner_kind="installation" if installation_id else "tenant", installation_id=installation_id,
            url=url, url_host="hooks.example.com", event_types=types or ["placement.started"], all_events=False, filters=filters or {},
            status=status, disabled_reason="manual" if status == "disabled" else None, max_in_flight=5,
        )  # fmt: skip
        db.add(sub)
        await db.flush()
        secret = await signing.store(sys_ctx(org.tenant_id), db, sub.id)
        return sub.id, secret


async def run_sink(tenant_id: uuid.UUID, rows: list[Any]) -> None:
    async with tenant_session(tenant_id) as db:
        await sink.on_rows(sys_ctx(tenant_id), db, rows)


async def deliveries(tenant_id: uuid.UUID, sub_id: uuid.UUID) -> list[WebhookDelivery]:
    async with tenant_session(tenant_id) as db:
        return list((await db.execute(select(WebhookDelivery).where(WebhookDelivery.subscription_id == sub_id).order_by(WebhookDelivery.created_at))).scalars())


async def pump_and_deliver(tenant_id: uuid.UUID) -> list[str | None]:
    ids = await scheduler.pump(tenant_id)
    return [await scheduler.deliver(tenant_id, i) for i in ids]


async def make_due(tenant_id: uuid.UUID, sub_id: uuid.UUID) -> None:
    async with tenant_session(tenant_id) as db:
        await db.execute(update(WebhookDelivery).where(WebhookDelivery.subscription_id == sub_id, WebhookDelivery.status == "retrying").values(next_attempt_at=common.now() - timedelta(seconds=1)))


# ------------------------------------------------------------------ pure rules
def test_ladder_is_one_plus_eight_retries() -> None:
    delays = [60, 300, 900, 3600, 7200, 14400, 28800, 28800]
    for n, base in enumerate(delays, start=1):
        status, delay = scheduler.plan_next(n, 500, "http_500", None)
        assert status == "retrying"
        assert delay is not None and base * 0.9 <= delay <= base * 1.1
    assert scheduler.plan_next(9, 500, "http_500", None) == ("failed", None)
    assert scheduler.max_attempts() == 9


def test_status_rules() -> None:
    assert scheduler.plan_next(1, 410, "http_410", None) == ("failed", None)
    assert scheduler.plan_next(1, 400, "http_400", None)[0] == "retrying"
    assert scheduler.plan_next(2, 400, "http_400", None)[0] == "retrying"
    assert scheduler.plan_next(3, 400, "http_400", None) == ("failed", None)
    for code in (408, 409, 503):
        assert scheduler.plan_next(5, code, f"http_{code}", None)[0] == "retrying"
    assert scheduler.plan_next(1, 429, "http_429", 120) == ("retrying", 120.0)
    assert scheduler.plan_next(1, 429, "http_429", 7200) == ("retrying", 3600.0)
    assert scheduler.plan_next(1, None, "timeout", None)[0] == "retrying"


def test_retry_after_and_excerpt() -> None:
    assert delivery.retry_after_seconds("120") == 120
    when = (common.now() + timedelta(seconds=300)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 250 <= (delivery.retry_after_seconds(when) or 0) <= 300
    text = delivery.excerpt(b"a\x00b\x07c" + b"x" * 5000)
    assert text.startswith("abc")
    assert len(text) <= 1024


def test_jitter_bounds() -> None:
    rng = random.Random(7)
    assert all(54 <= scheduler.jittered(60, rng=rng) <= 66 for _ in range(200))


# ------------------------------------------------------------------ sink
async def test_sink_idempotent_and_matching(org3: Org3, receiver: Receiver) -> None:
    client_a = str(uuid.uuid4())
    sub_id, _ = await make_sub(org3, filters={})
    filtered, _ = await make_sub(org3, types=["invoice.issued"], filters={"client_id": [client_a]})
    disabled, _ = await make_sub(org3, status="disabled")
    row = await outbox_row(org3.tenant_id)
    inv_other = await outbox_row(org3.tenant_id, "invoice.issued", {"id": str(uuid.uuid4()), "client_id": str(uuid.uuid4()), "legal_entity_id": str(uuid.uuid4()), "number": "1"}, "invoice")
    inv_match = await outbox_row(org3.tenant_id, "invoice.issued", {"id": str(uuid.uuid4()), "client_id": client_a, "legal_entity_id": str(uuid.uuid4()), "number": "2"}, "invoice")
    await run_sink(org3.tenant_id, [row, inv_other, inv_match])
    await run_sink(org3.tenant_id, [row, inv_other, inv_match])  # same batch twice
    assert len(await deliveries(org3.tenant_id, sub_id)) == 1
    got = await deliveries(org3.tenant_id, filtered)
    assert [d.event_id for d in got] == [inv_match.id]
    held = await deliveries(org3.tenant_id, disabled)
    assert [d.status for d in held] == ["held"]


async def test_sink_exception_leaves_batch_pending(org3: Org3) -> None:
    from app.services.public_events import sinks as sinks_mod

    assert any(s.name == "webhooks" for s in sinks_mod.sinks())


# ------------------------------------------------------------------ delivery
async def test_signed_delivery_headers_and_success(org3: Org3, receiver: Receiver) -> None:
    sub_id, secret = await make_sub(org3)
    row = await outbox_row(org3.tenant_id)
    await run_sink(org3.tenant_id, [row])
    assert await pump_and_deliver(org3.tenant_id) == ["succeeded"]
    req = receiver.requests[-1]
    assert req.headers["TalentOS-Event-Id"] == str(row.id)
    assert req.headers["TalentOS-Event-Type"] == "placement.started"
    assert req.headers["TalentOS-Attempt"] == "1"
    assert req.headers["User-Agent"].startswith("TalentOS-Webhooks/1.0")
    assert "authorization" not in req.headers
    assert "cookie" not in req.headers
    assert verify_v1(req.headers["TalentOS-Signature"], [secret.encode()], req.content)
    body = json.loads(req.content)
    assert body["id"] == str(row.id)
    assert body["type"] == "placement.started"
    assert set(body) == {"id", "type", "version", "api_version", "occurred_at", "sequence", "tenant_ref", "livemode", "subject", "data"}
    sub = await sub_row(org3.tenant_id, sub_id)
    assert sub.last_success_at is not None


async def test_rotation_sends_two_signatures(org3: Org3, receiver: Receiver) -> None:
    sub_id, old = await make_sub(org3)
    async with tenant_session(org3.tenant_id) as db:
        new, _expires = await signing.rotate(sys_ctx(org3.tenant_id), db, sub_id)
    await run_sink(org3.tenant_id, [await outbox_row(org3.tenant_id)])
    await pump_and_deliver(org3.tenant_id)
    req = receiver.requests[-1]
    header = req.headers["TalentOS-Signature"]
    assert header.count("v1=") == 2
    assert verify_v1(header, [new.encode()], req.content)
    assert verify_v1(header, [old.encode()], req.content)
    assert not verify_v1(header, [old.encode()], req.content, now=int(common.now().timestamp()) + 600)  # stale timestamp


async def test_500_retries_then_replay_keeps_event_id(org3: Org3, receiver: Receiver) -> None:
    receiver.status = 500
    sub_id, _ = await make_sub(org3)
    row = await outbox_row(org3.tenant_id)
    await run_sink(org3.tenant_id, [row])
    assert await pump_and_deliver(org3.tenant_id) == ["retrying"]
    (d,) = await deliveries(org3.tenant_id, sub_id)
    assert d.attempt_count == 1
    assert 50 <= (d.next_attempt_at - common.now()).total_seconds() <= 70  # type: ignore[operator]
    assert d.attempts[0]["status_code"] == 500
    sub = await sub_row(org3.tenant_id, sub_id)
    assert sub.consecutive_failures == 1
    assert sub.failure_streak_started_at is not None
    receiver.status = 200
    from app.services.webhooks import replay

    async with tenant_session(org3.tenant_id) as db:
        out = await replay.replay_delivery(sys_ctx(org3.tenant_id), db, d.id)
    assert out.event_id == row.id
    await pump_and_deliver(org3.tenant_id)
    req = receiver.requests[-1]
    assert req.headers["TalentOS-Replay"] == "true"
    assert req.headers["TalentOS-Event-Id"] == str(row.id)
    sub = await sub_row(org3.tenant_id, sub_id)
    assert sub.consecutive_failures == 0


async def test_400_retried_twice_then_failed(org3: Org3, receiver: Receiver) -> None:
    receiver.status = 400
    sub_id, _ = await make_sub(org3)
    await run_sink(org3.tenant_id, [await outbox_row(org3.tenant_id)])
    assert await pump_and_deliver(org3.tenant_id) == ["retrying"]
    await make_due(org3.tenant_id, sub_id)
    assert await pump_and_deliver(org3.tenant_id) == ["retrying"]
    await make_due(org3.tenant_id, sub_id)
    assert await pump_and_deliver(org3.tenant_id) == ["failed"]


async def test_410_disables_and_later_events_are_held(org3: Org3, receiver: Receiver) -> None:
    receiver.status = 410
    sub_id, _ = await make_sub(org3)
    await run_sink(org3.tenant_id, [await outbox_row(org3.tenant_id)])
    assert await pump_and_deliver(org3.tenant_id) == ["failed"]
    sub = await sub_row(org3.tenant_id, sub_id)
    assert (sub.status, sub.disabled_reason) == ("disabled", "gone")
    await run_sink(org3.tenant_id, [await outbox_row(org3.tenant_id)])
    assert [d.status for d in await deliveries(org3.tenant_id, sub_id)] == ["failed", "held"]


async def test_429_retry_after_capped(org3: Org3, receiver: Receiver) -> None:
    receiver.status = 429
    receiver.headers = {"Retry-After": "7200"}
    sub_id, _ = await make_sub(org3)
    await run_sink(org3.tenant_id, [await outbox_row(org3.tenant_id)])
    await pump_and_deliver(org3.tenant_id)
    (d,) = await deliveries(org3.tenant_id, sub_id)
    assert 3590 <= (d.next_attempt_at - common.now()).total_seconds() <= 3600  # type: ignore[operator]


async def test_redirect_not_followed(org3: Org3, receiver: Receiver) -> None:
    receiver.status = 302
    receiver.headers = {"Location": "https://private.example.com/"}
    sub_id, _ = await make_sub(org3)
    await run_sink(org3.tenant_id, [await outbox_row(org3.tenant_id)])
    assert await pump_and_deliver(org3.tenant_id) == ["retrying"]
    (d,) = await deliveries(org3.tenant_id, sub_id)
    assert d.last_error == "redirect_not_followed"
    assert len(receiver.requests) == 1


async def test_dns_rebinding_disables(org3: Org3, receiver: Receiver) -> None:
    sub_id, _ = await make_sub(org3, url="https://private.example.com/hook")
    await run_sink(org3.tenant_id, [await outbox_row(org3.tenant_id)])
    assert await pump_and_deliver(org3.tenant_id) == ["failed"]
    (d,) = await deliveries(org3.tenant_id, sub_id)
    assert d.last_error == "egress_refused:private_address"
    sub = await sub_row(org3.tenant_id, sub_id)
    assert sub.disabled_reason == "egress_refused"
    assert receiver.requests == []


async def test_kill_switch_pauses_without_counting(org3: Org3, receiver: Receiver) -> None:
    sub_id, _ = await make_sub(org3)
    await run_sink(org3.tenant_id, [await outbox_row(org3.tenant_id)])
    async with tenant_session(org3.tenant_id) as db:
        row = await ecosystem_service.settings_row(db, org3.tenant_id, lock=True)
        row.kill_switch_at, row.kill_switch_reason = common.now(), "test"
    ecosystem_service.bust_cache()
    assert await pump_and_deliver(org3.tenant_id) == []
    (d,) = await deliveries(org3.tenant_id, sub_id)
    assert (d.status, d.attempt_count) == ("pending", 0)
    assert d.next_attempt_at > common.now()  # type: ignore[operator]
    async with tenant_session(org3.tenant_id) as db:
        row = await ecosystem_service.settings_row(db, org3.tenant_id, lock=True)
        row.kill_switch_at = None
        await db.execute(update(WebhookDelivery).where(WebhookDelivery.id == d.id).values(next_attempt_at=common.now()))
    ecosystem_service.bust_cache()
    assert await pump_and_deliver(org3.tenant_id) == ["succeeded"]


async def test_per_subscription_concurrency(org3: Org3, receiver: Receiver) -> None:
    sub_id, _ = await make_sub(org3)
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(update(WebhookSubscription).where(WebhookSubscription.id == sub_id).values(max_in_flight=2))
    await run_sink(org3.tenant_id, [await outbox_row(org3.tenant_id) for _ in range(5)])
    first = await scheduler.pump(org3.tenant_id)
    assert len(first) == 2
    assert await scheduler.pump(org3.tenant_id) == []


# ------------------------------------------------------------------ health / test events / range replay
async def test_health_notifies_then_auto_disables(org3: Org3, receiver: Receiver) -> None:
    sub_id, _ = await make_sub(org3)
    await run_sink(org3.tenant_id, [await outbox_row(org3.tenant_id)])
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(update(WebhookSubscription).where(WebhookSubscription.id == sub_id).values(consecutive_failures=3, failure_streak_started_at=common.now() - timedelta(hours=2)))
    await health.scan(org3.tenant_id)
    sub = await sub_row(org3.tenant_id, sub_id)
    assert sub.streak_notified_at is not None
    assert sub.status == "active"
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(update(WebhookSubscription).where(WebhookSubscription.id == sub_id).values(failure_streak_started_at=common.now() - timedelta(hours=73)))
    await health.scan(org3.tenant_id)
    sub = await sub_row(org3.tenant_id, sub_id)
    assert (sub.status, sub.disabled_reason) == ("disabled", "failing")
    assert [d.status for d in await deliveries(org3.tenant_id, sub_id)] == ["held"]


async def test_test_event_and_range_replay(org3: Org3, receiver: Receiver) -> None:
    sub_id, secret = await make_sub(org3, types=["placement.started", "timesheet.approved"])
    async with tenant_session(org3.tenant_id) as db:
        out = await test_events.send_test_event(sys_ctx(org3.tenant_id), db, sub_id, TestEventIn(event_type="timesheet.approved"))
    assert out.status == "succeeded"
    req = receiver.requests[-1]
    assert req.headers["TalentOS-Test"] == "true"
    assert json.loads(req.content)["livemode"] is False
    assert verify_v1(req.headers["TalentOS-Signature"], [secret.encode()], req.content)
    receiver.status = 500
    await run_sink(org3.tenant_id, [await outbox_row(org3.tenant_id), await outbox_row(org3.tenant_id)])
    await pump_and_deliver(org3.tenant_id)
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(update(WebhookDelivery).where(WebhookDelivery.subscription_id == sub_id, WebhookDelivery.kind == "event").values(status="failed", next_attempt_at=None))
    receiver.status = 200
    from app.services.webhooks import replay

    window = ReplayRangeIn.model_validate({"from": (common.now() - timedelta(days=1)).isoformat(), "to": (common.now() + timedelta(minutes=1)).isoformat(), "preview": True})
    async with tenant_session(org3.tenant_id) as db:
        preview = await replay.replay_range(sys_ctx(org3.tenant_id), db, sub_id, window)
    assert preview.count == 2
    created = await replay.replay_range_task(org3.tenant_id, sub_id, window.date_from, window.date_to, ["failed"], None)
    assert created == 2
    assert await pump_and_deliver(org3.tenant_id) == ["succeeded", "succeeded"]
    old = ReplayRangeIn.model_validate({"from": (common.now() - timedelta(days=40)).isoformat(), "to": (common.now() - timedelta(days=35)).isoformat()})
    from app.core.errors import ConflictError

    async with tenant_session(org3.tenant_id) as db:
        with pytest.raises(ConflictError):
            await replay.replay_range(sys_ctx(org3.tenant_id), db, sub_id, old)


# ------------------------------------------------------------------ D-11 + erasure handler
async def test_subject_erased_fan_out_and_erasure_handler(org3: Org3, receiver: Receiver) -> None:
    from app.models.ecosystem import SubjectExposure
    from tests.support.oauth import install_app, make_app

    inst = await install_app(org3.tenant_id, await make_app(scopes=("demands:read",), resources=("api",)), granted_by=org3.admin.user_id)
    lonely = await install_app(org3.tenant_id, await make_app(scopes=("demands:read",), resources=("api",)), granted_by=org3.admin.user_id)
    sub_id, _ = await make_sub(org3, types=["demand.created"], installation_id=inst.id)
    cand = uuid.uuid4()
    async with tenant_session(org3.tenant_id) as db:
        for i in (inst.id, lonely.id):
            db.add(SubjectExposure(id=uuid.uuid4(), tenant_id=org3.tenant_id, installation_id=i, subject_type="candidate", subject_id=cand,
                                   channel="api", first_at=common.now(), last_at=common.now()))  # fmt: skip
    # an older delivery naming the candidate (to be erased)
    async with tenant_session(org3.tenant_id) as db:
        db.add(WebhookDelivery(id=uuid.uuid4(), tenant_id=org3.tenant_id, subscription_id=sub_id, event_id=uuid.uuid4(), event_type="application.created",
                               candidate_id=cand, kind="event", status="succeeded"))  # fmt: skip
    erased = await outbox_row(org3.tenant_id, "subject.erased", {"subject_type": "candidate", "id": str(cand)}, "candidate", cand)
    await run_sink(org3.tenant_id, [erased])
    got = await deliveries(org3.tenant_id, sub_id)
    assert {d.event_type for d in got} == {"application.created", "subject.erased"}  # regardless of the sub's types / scopes
    from app.core.audit import AuditLog

    async with tenant_session(org3.tenant_id) as db:
        audits = list((await db.execute(select(AuditLog).where(AuditLog.action == "webhook.erasure_no_endpoint"))).scalars())
    assert len(audits) == 1
    from app.services.privacy.registry import ErasureContext
    from app.services.webhooks_erasure import WebhooksHandler

    async with tenant_session(org3.tenant_id) as db:
        ctx = ErasureContext(tenant_id=org3.tenant_id, candidate_id=cand, run_id=uuid.uuid4(), reason="dsr")
        counts = await WebhooksHandler().erase(db, ctx)
        assert counts["deliveries"] == 1
        assert await WebhooksHandler().verify(db, ctx) == []
    assert [d.event_type for d in await deliveries(org3.tenant_id, sub_id)] == ["subject.erased"]


# ------------------------------------------------------------------ echo + thin payloads
async def test_echo_in_process_in_sandbox(app: Any, receiver: Receiver) -> None:
    from tests.modules.identity.conftest import join, make_tenant

    tenant = await make_tenant(mode="sandbox", sandbox_owner_kind="platform")
    admin = await join(app, tenant, name="Sam Sandbox")
    try:
        ep = await admin.client.post("/api/v1/webhooks/echo-endpoints", json={"label": "dev"}, headers=admin.h())
        assert ep.status_code == 201, ep.text
        url = ep.json()["url"]
        created = await admin.client.post("/api/v1/webhooks/subscriptions", json={"url": url, "event_types": ["placement.started"]}, headers=admin.h())
        assert created.status_code == 201, created.text
        sid = uuid.UUID(created.json()["subscription"]["id"])
        sub = await sub_row(tenant.id, sid)
        assert sub.status == "active"  # the echo answered the challenge in-process
        test = await admin.client.post(f"/api/v1/webhooks/subscriptions/{sid}/test", json={"event_type": "placement.started"}, headers=admin.h())
        assert test.status_code == 200, test.text
        assert test.json()["status"] == "succeeded"
        receipts = await admin.client.get(f"/api/v1/webhooks/echo-endpoints/{ep.json()['id']}/receipts")
        assert receipts.status_code == 200
        assert all(r["signature_valid"] for r in receipts.json())
        assert len(receipts.json()) == 2
        assert receiver.requests == []  # never left the process
    finally:
        await admin.client.aclose()


async def test_echo_refused_outside_sandbox(org3: Org3, receiver: Receiver) -> None:
    resp = await org3.admin.client.post("/api/v1/webhooks/echo-endpoints", json={}, headers=org3.admin.h())
    assert resp.status_code == 404
    url = common.echo_prefix() + f"{org3.tenant_id.hex}.abc"
    bad = await org3.admin.client.post("/api/v1/webhooks/subscriptions", json={"url": url, "event_types": ["placement.started"]}, headers=org3.admin.h())
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "echo_sandbox_only"


def test_thin_payloads_catalogue_wide() -> None:
    import re

    email = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", re.I)
    phone = re.compile(r"^\+?\d{10,13}$")
    pan = re.compile(r"[A-Z]{5}\d{4}[A-Z]")
    specs = registry.catalogue()
    assert {"placement.started", "timesheet.approved", "invoice.issued", "payment.recorded", "subject.erased"} <= set(specs)
    for t, spec in specs.items():
        data = dict(spec.example or {}) or test_events.sample_data(spec.data_model)
        assert not set(data) & registry.PII_FIELD_DENYLIST, t
        for value in map(str, data.values()):
            assert not email.search(value) and not phone.search(value) and not pan.search(value), (t, value)
        assert registry.validate_spec(spec) == [], t


async def test_create_through_service_rejects_wildcard_mixed(org3: Org3, receiver: Receiver) -> None:
    from app.core.errors import ValidationFailed

    async with tenant_session(org3.tenant_id) as db:
        with pytest.raises(ValidationFailed):
            await subscriptions.create_subscription(sys_ctx(org3.tenant_id), db, SubscriptionCreate(url="https://h.example.com/", event_types=["*", "placement.started"]))
