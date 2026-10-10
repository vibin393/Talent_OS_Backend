# ruff: noqa: E501
"""Epic 33 FR-172/173: SMS / WhatsApp templates, sends, opt-outs, consent, quiet hours, webhooks, manual queue."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.comms import ChannelMessage
from app.models.connectors import ConnectorJob
from app.services.comms.channel_settings import quiet_until
from app.services.connectors.sms.fake import FakeSmsProvider
from app.services.connectors.whatsapp.fake import FakeWhatsAppProvider
from app.services.messaging import opt_outs, sending
from tests.epic10 import DOrg, h
from tests.modules.comms_p2.helpers import (
    COMMS,
    PHONE,
    attest,
    candidate,
    connector,
    enable,
    send,
    template,
    webhook,
)


async def _message(tenant_id: uuid.UUID, message_id: str) -> ChannelMessage:
    async with tenant_session(tenant_id) as db:
        row = await db.get(ChannelMessage, uuid.UUID(message_id))
        assert row is not None
        return row


async def _activities(tenant_id: uuid.UUID, kind: str) -> list[dict]:
    async with tenant_session(tenant_id) as db:
        return list(
            (
                await db.execute(text("SELECT body, data FROM activities WHERE kind = :k"), {"k": kind})
            ).mappings()
        )


async def test_unregistered_template_409_and_validation(dorg: DOrg) -> None:
    m = dorg.admin
    await enable(m)
    draft = await template(m, status="draft")
    assert draft["sendable"] is False
    cid = await candidate(m)
    resp = await send(m, draft["id"], cid)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "template_not_registered"
    bad = await m.client.post(
        f"{COMMS}/channel-templates",
        json={"channel": "sms", "key": "bad", "name": "Bad", "purpose": "other_transactional", "body": "x {#var#}",
              "variables": ["candidate.first_name"], "provider_template_ref": "123", "status": "registered"},
        headers=h(m),
    )  # fmt: skip
    assert bad.status_code == 422


async def test_sms_send_activity_without_phone_and_idempotent_job(dorg: DOrg) -> None:
    m = dorg.admin
    await enable(m)
    await connector(m)
    t = await template(m)
    cid = await candidate(m)
    resp = await send(m, t["id"], cid, key=str(uuid.uuid4()))
    assert resp.status_code == 200, resp.text
    (result,) = resp.json()["results"]
    assert result["status"] == "queued"
    msg = await _message(dorg.tenant_id, result["message_id"])
    assert msg.status == "sent" and msg.provider_message_id
    (spec,) = FakeSmsProvider.sends()
    assert (
        spec.to_e164 == PHONE
        and spec.variables == ["Priya"]
        and spec.template_ref == t["provider_template_ref"]
    )
    assert spec.body.startswith("Hi Priya, please submit your timesheet.")
    acts = await _activities(dorg.tenant_id, "sms")
    assert acts and all(PHONE not in (a["body"] + str(a["data"])) for a in acts)
    # enqueue twice -> one job, one send
    ctx = RequestContext(tenant_id=dorg.tenant_id)
    async with tenant_session(dorg.tenant_id) as db:
        row = await db.get(ChannelMessage, msg.id)
        assert row is not None
        await sending.enqueue_dispatch(ctx, db, row)
    async with tenant_session(dorg.tenant_id) as db:
        jobs = list(
            (await db.execute(select(ConnectorJob).where(ConnectorJob.subject_id == msg.id))).scalars()
        )
        assert len(jobs) == 1 and jobs[0].idempotency_key == f"sms:send:channel_message:{msg.id}:1"
    assert len(FakeSmsProvider.sends()) == 1


async def test_refusals_no_phone_opted_out_no_consent(dorg: DOrg) -> None:
    m = dorg.admin
    await enable(m)
    await connector(m)
    await connector(m, "whatsapp")
    sms = await template(m)
    wa = await template(m, "whatsapp")
    no_phone = await candidate(m, phone=None)
    opted = await candidate(m, phone="+919811112222")
    add = await m.client.post(
        f"{COMMS}/opt-outs", json={"channel": "sms", "phone_e164": "+919811112222"}, headers=h(m)
    )
    assert add.status_code == 201 and "+919811112222" not in add.text
    resp = await send(m, sms["id"], no_phone, opted)
    reasons = {r["entity_id"]: r["reason"] for r in resp.json()["results"]}
    assert reasons == {str(no_phone): "no_phone", str(opted): "opted_out"}
    plain = await candidate(m)
    wa_resp = await send(m, wa["id"], plain, channel="whatsapp")
    assert wa_resp.json()["results"][0]["reason"] == "no_whatsapp_consent"
    assert FakeSmsProvider.sends() == [] and FakeWhatsAppProvider.sends() == []
    elig = await m.client.post(
        f"{COMMS}/messages/eligibility",
        json={
            "channel": "sms",
            "template_id": sms["id"],
            "recipients": [{"entity_type": "candidate", "entity_id": str(plain)}],
        },
        headers=h(m),
    )
    assert elig.json()[0]["eligible"] is True and elig.json()[0]["preview"].startswith("Hi Priya")


async def test_opt_out_between_enqueue_and_task_is_honoured(dorg: DOrg) -> None:
    m = dorg.admin
    await enable(m)
    await connector(m)
    t = await template(m)
    cid = await candidate(m)
    ctx = RequestContext(tenant_id=dorg.tenant_id)
    # queue without dispatching, then opt out, then run the gate as the task would
    async with tenant_session(dorg.tenant_id) as db:
        message = ChannelMessage(
            id=uuid.uuid4(), tenant_id=dorg.tenant_id, channel="sms", direction="outbound", template_id=uuid.UUID(t["id"]),
            recipient_kind="candidate", recipient_id=cid, candidate_id=cid, to_e164=PHONE, body_rendered="Hi",
            variables={"values": ["Priya"]}, status="queued", origin="user",
        )  # fmt: skip
        db.add(message)
    async with tenant_session(dorg.tenant_id) as db:
        await opt_outs.record(db, "sms", PHONE, "user")
    async with tenant_session(dorg.tenant_id) as db:
        row = await db.get(ChannelMessage, message.id)
        assert row is not None
        await sending.enqueue_dispatch(ctx, db, row)
    msg = await _message(dorg.tenant_id, str(message.id))
    assert msg.status == "refused" and msg.refusal_reason == "opted_out"
    assert FakeSmsProvider.sends() == []


async def test_stop_keyword_and_whatsapp_consent_window(dorg: DOrg) -> None:
    m = dorg.admin
    await enable(m)
    sms_cfg = await connector(m)
    wa_cfg = await connector(m, "whatsapp")
    sms = await template(m)
    wa = await template(m, "whatsapp")
    cid = await candidate(m)
    await attest(dorg, cid)
    first = (await send(m, wa["id"], cid, channel="whatsapp")).json()["results"][0]
    assert first["status"] == "queued"
    sent = await _message(dorg.tenant_id, first["message_id"])
    assert sent.status == "sent"
    # delivery receipt + matched reply within 24 h
    bad = await webhook(m, "whatsapp", wa_cfg["id"], [], sign=False)
    assert bad.status_code == 401
    ok = await webhook(m, "whatsapp", wa_cfg["id"], [
        {"kind": "delivery", "provider_message_id": sent.provider_message_id, "status": "delivered"},
        {"kind": "inbound", "provider_message_id": "wamid.in-1", "from_e164": PHONE, "text": "Thanks!"},
    ])  # fmt: skip
    assert ok.status_code == 202, ok.text
    assert (await _message(dorg.tenant_id, first["message_id"])).status == "delivered"
    async with tenant_session(dorg.tenant_id) as db:
        inbound = (
            await db.execute(select(ChannelMessage).where(ChannelMessage.provider_message_id == "wamid.in-1"))
        ).scalar_one()
        assert (
            inbound.direction == "inbound"
            and inbound.recipient_id == cid
            and inbound.in_reply_to_id == sent.id
        )
    # unmatched: a number never messaged
    await webhook(
        m,
        "whatsapp",
        wa_cfg["id"],
        [{"kind": "inbound", "provider_message_id": "wamid.x", "from_e164": "+919800000001", "text": "Hi"}],
    )
    unmatched = (await m.client.get(f"{COMMS}/inbound/unmatched")).json()
    assert [u["recipient_id"] for u in unmatched] == [None]
    # STOP on WhatsApp withdraws the consent -> next send refused
    await webhook(
        m,
        "whatsapp",
        wa_cfg["id"],
        [{"kind": "inbound", "provider_message_id": "wamid.stop", "from_e164": PHONE, "text": "STOP"}],
    )
    again = (await send(m, wa["id"], cid, channel="whatsapp")).json()["results"][0]
    assert again["reason"] in ("no_whatsapp_consent", "opted_out")
    # SMS STOP -> opted_out
    await send(m, sms["id"], cid)
    await webhook(m, "sms", sms_cfg["id"], [{"kind": "inbound", "from_e164": PHONE, "text": "stop"}])
    refused = (await send(m, sms["id"], cid)).json()["results"][0]
    assert refused["reason"] == "opted_out"


async def test_retries_then_failure_and_manual_queue(dorg: DOrg) -> None:
    m = dorg.admin
    await enable(m)
    await connector(m)
    t = await template(m)
    cid = await candidate(m)
    FakeSmsProvider.fail_sends = 10
    result = (await send(m, t["id"], cid)).json()["results"][0]
    msg = await _message(dorg.tenant_id, result["message_id"])
    assert msg.status == "failed" and msg.error_code == "provider_unavailable"
    async with tenant_session(dorg.tenant_id) as db:
        job = (await db.execute(select(ConnectorJob).where(ConnectorJob.subject_id == msg.id))).scalar_one()
        assert job.status == "dead" and job.attempts == 3
    FakeSmsProvider.reset()
    # Manual connector (no config for whatsapp -> manual)
    wa = await template(m, "whatsapp")
    await attest(dorg, cid)
    queued = (await send(m, wa["id"], cid, channel="whatsapp")).json()["results"][0]
    assert (await _message(dorg.tenant_id, queued["message_id"])).status == "manual_pending"
    queue = (await m.client.get(f"{COMMS}/manual-queue")).json()
    assert [q["id"] for q in queue] == [queued["message_id"]] and queue[0]["to_e164"] == PHONE
    done = await m.client.post(f"{COMMS}/manual-queue/{queued['message_id']}/mark-sent", headers=h(m))
    assert done.status_code == 200 and done.json()["status"] == "sent"
    again = await m.client.post(f"{COMMS}/manual-queue/{queued['message_id']}/mark-sent", headers=h(m))
    assert again.status_code == 409
    assert any("WhatsApp sent" in a["body"] for a in await _activities(dorg.tenant_id, "whatsapp"))


async def test_quiet_hours_defer_and_release(dorg: DOrg) -> None:
    window = {"start": "21:00", "end": "09:00", "tz": "Asia/Kolkata"}
    late = datetime(2026, 10, 5, 17, 0, tzinfo=UTC)  # 22:30 IST
    until = quiet_until(window, late)
    assert until is not None and until.astimezone(UTC) == datetime(2026, 10, 6, 3, 30, tzinfo=UTC)
    assert quiet_until(window, datetime(2026, 10, 5, 6, 0, tzinfo=UTC)) is None  # 11:30 IST
    m = dorg.admin
    await enable(m, quiet_hours={"start": "00:00", "end": "23:59", "tz": "Asia/Kolkata"})
    await connector(m)
    t = await template(m)
    cid = await candidate(m)
    result = (await send(m, t["id"], cid)).json()["results"][0]
    msg = await _message(dorg.tenant_id, result["message_id"])
    assert msg.status == "deferred_quiet_hours" and FakeSmsProvider.sends() == []
    await enable(m)  # quiet hours off
    ctx = RequestContext(tenant_id=dorg.tenant_id)
    async with tenant_session(dorg.tenant_id) as db:
        assert await sending.release_deferred(ctx, db, now=msg.deferred_until) == 1
    msg = await _message(dorg.tenant_id, result["message_id"])
    assert msg.status == "sent" and msg.dispatch_version == 2 and len(FakeSmsProvider.sends()) == 1


async def test_opt_out_link_and_portal_messages(dorg: DOrg) -> None:
    from app.core import secure_links

    m = dorg.admin
    await enable(m)
    await connector(m)
    t = await template(m)
    cid = await candidate(m)
    result = (await send(m, t["id"], cid)).json()["results"][0]
    msg = await _message(dorg.tenant_id, result["message_id"])
    token = msg.body_rendered.rsplit("/l/", 1)[1].strip()
    view = await m.client.get(f"/api/v1/public/links/{token}")
    assert view.status_code == 200 and view.json()["channel"] == "sms" and view.json()["status"] == "active"
    from app.core.settings import get_settings

    post = await m.client.post(
        f"/api/v1/public/links/{token}/opt-out", headers={"Origin": get_settings().frontend_url}
    )
    assert post.status_code == 200 and post.json()["status"] == "opted_out"
    async with tenant_session(dorg.tenant_id) as db:
        assert await opt_outs.is_opted_out(db, "sms", PHONE)
    assert secure_links


async def test_workflow_action_runs_as_workflow_and_obeys_refusals(dorg: DOrg) -> None:
    from app.services.workflow import engine as workflow

    m = dorg.admin
    await enable(m)
    await connector(m)
    t = await template(m)
    ok_cid = await candidate(m)
    no_phone = await candidate(m, phone=None, name="Nora Nophone")
    from app.models.workflow import WorkflowRule

    async with tenant_session(dorg.tenant_id) as db:
        db.add(
            WorkflowRule(
                id=uuid.uuid4(), tenant_id=dorg.tenant_id, name="sms rule", trigger="candidate.created", conditions={},
                actions=[{"type": "comms.send_sms", "params": {"template_id": t["id"], "to": "worker"}}],
                is_active=True, version=1, created_by=m.user_id,
            )
        )  # fmt: skip
    for cid in (ok_cid, no_phone):
        async with tenant_session(dorg.tenant_id) as db:
            await workflow.evaluate_event(db, "candidate.created", "candidate", cid, uuid.uuid4(), {}, 0)
    async with tenant_session(dorg.tenant_id) as db:
        rows = list(
            (await db.execute(select(ChannelMessage).where(ChannelMessage.origin == "workflow"))).scalars()
        )
    assert [r.recipient_id for r in rows] == [ok_cid]
    async with tenant_session(dorg.tenant_id) as db:
        errors = (
            await db.execute(
                text("SELECT count(*) FROM workflow_executions WHERE action_results::text LIKE '%no_phone%'")
            )
        ).scalar_one()
    assert errors >= 1
