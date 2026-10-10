# ruff: noqa: E501
"""Epic 39 stories 39.1 / 39.3 (FR-196, FR-197): audiences, preview, lifecycle, approval, A/B, cancel, send path."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from app.models.campaigns import Campaign, CampaignMessage
from app.models.comms import Email
from app.schemas.approvals import DecideIn
from app.services import approvals, comms_service
from app.services.campaigns import campaigns as campaign_svc
from app.services.campaigns import sending
from app.services.connectors.sms.fake import FakeSmsProvider
from tests.epic5 import h
from tests.modules.campaigns.helpers import (
    API,
    audience,
    candidate,
    email_campaign,
    grant,
    messages,
    setup_marketing,
    submit,
    withdraw,
)
from tests.modules.comms_p2 import helpers as p2
from tests.modules.demands.helpers import DOrg
from tests.modules.identity.conftest import join


async def test_audience_preview_counts_reachable_and_suppressed_by_reason(dorg: DOrg) -> None:
    m = dorg.admin
    a = await candidate(m, "Asha", phone="+919876511111")
    b = await candidate(m, "Bala", consents=())
    c = await candidate(m, "Chitra", email=None)
    d = await candidate(m, "Dev")
    aud = await audience(m, [a, b, c, d], exclude=[d])
    resp = await m.client.post(f"{API}/audiences/{aud['id']}/preview", headers=h(m))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert body["reachable"] == {"email": 1, "sms": 1, "whatsapp": 0}
    assert body["suppressed"]["email"] == {"no_marketing_consent": 1, "no_address": 1}
    assert body["suppressed"]["whatsapp"]["no_whatsapp_optin"] == 2  # optin is checked before the address


async def test_list_import_matches_existing_candidates_only(dorg: DOrg) -> None:
    m = dorg.admin
    await candidate(m, "Known", email="known@example.com")
    aud = await audience(m, [])
    csv_body = b"email\nknown@example.com\nstranger@example.com\n"
    files = {"file": ("list.csv", csv_body, "text/csv")}
    preview = await m.client.post(
        f"{API}/audiences/{aud['id']}/list-import", params={"mode": "include"}, files=files, headers=h(m)
    )
    assert preview.status_code == 200, preview.text
    assert (preview.json()["matched"], preview.json()["unmatched"], preview.json()["applied"]) == (
        1,
        1,
        False,
    )
    applied = await m.client.post(
        f"{API}/audiences/{aud['id']}/list-import",
        params={"mode": "include", "apply": "true"},
        files=files,
        headers=h(m),
    )
    assert applied.json()["applied"] is True
    got = (await m.client.get(f"{API}/audiences/{aud['id']}")).json()
    assert got["include_list_count"] == 1


async def test_email_campaign_sends_with_footer_unsubscribe_header_and_pixel_only_with_tracking(
    dorg: DOrg, outbox: Any
) -> None:
    m = dorg.admin
    await setup_marketing(m)
    tracked = await candidate(m, "Tara", consents=("marketing", "tracking"))
    plain = await candidate(m, "Uma")
    no_consent = await candidate(m, "Varun", consents=())
    aud = await audience(m, [tracked, plain, no_consent])
    camp = await email_campaign(m, aud["id"])
    resp = await submit(m, camp["id"])
    assert resp.status_code == 200, resp.text
    final = (await m.client.get(f"{API}/campaigns/{camp['id']}")).json()
    assert final["status"] in ("sending", "completed")
    assert final["counts"]["reachable"] == 2
    rows = {r.candidate_id: r for r in await messages(dorg.tenant_id, campaign_id=camp["id"])}
    assert (
        rows[no_consent].status == "suppressed"
        and rows[no_consent].suppression_reason == "no_marketing_consent"
    )
    assert rows[tracked].status == "sent" and rows[plain].status == "sent"
    assert rows[tracked].pixel is True and rows[plain].pixel is False
    sent = [e for e in outbox.sent if e.kind == "comms_campaign"]
    assert len(sent) == 2
    for e in sent:
        assert (
            "List-Unsubscribe" in e.headers
            and e.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        )
        assert "Initech Talent, 1 MG Road" in (e.html or "")
    assert sum(1 for e in sent if "o.gif" in (e.html or "")) == 1
    # completion beat
    from app.tasks import campaigns as tasks

    await tasks.tick(dorg.tenant_id)
    assert (await m.client.get(f"{API}/campaigns/{camp['id']}")).json()["status"] == "completed"
    analytics = (await m.client.get(f"{API}/campaigns/{camp['id']}/analytics")).json()
    assert analytics["counts"]["sent"] == 2 and analytics["counts"]["delivered"] == 2
    assert analytics["suppressed_by_reason"] == {"no_marketing_consent": 1}
    page = (await m.client.get(f"{API}/campaigns/{camp['id']}/messages", params={"status": "sent"})).json()
    assert page["total"] == 2 and {"candidate_name", "status"} <= set(page["items"][0])
    assert "body" not in page["items"][0]


async def test_footer_contact_required_and_invalid_transitions(dorg: DOrg) -> None:
    m = dorg.admin
    cid = await candidate(m, "Asha")
    aud = await audience(m, [cid])
    camp = await email_campaign(m, aud["id"])
    resp = await submit(m, camp["id"])
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "footer_contact_required"
    for action in ("pause", "resume"):
        r = await m.client.post(f"{API}/campaigns/{camp['id']}/{action}", headers=h(m))
        assert r.status_code == 409, r.text
    await setup_marketing(m)
    future = "2030-01-01T09:00:00+05:30"
    ok = await submit(m, camp["id"], scheduled_at=future)
    assert ok.json()["status"] == "scheduled"
    edit = await m.client.patch(f"{API}/campaigns/{camp['id']}", json={"name": "x"}, headers=h(m))
    assert edit.status_code == 409
    again = await submit(m, camp["id"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "invalid_transition"
    paused = await m.client.post(f"{API}/campaigns/{camp['id']}/pause", headers=h(m))
    assert paused.json()["status"] == "paused"
    resumed = await m.client.post(f"{API}/campaigns/{camp['id']}/resume", headers=h(m))
    assert resumed.json()["status"] == "scheduled"
    cancelled = await m.client.post(
        f"{API}/campaigns/{camp['id']}/cancel", json={"reason": "Changed plan"}, headers=h(m)
    )
    assert cancelled.json()["status"] == "cancelled"
    assert (
        await m.client.post(f"{API}/campaigns/{camp['id']}/cancel", json={"reason": "x"}, headers=h(m))
    ).status_code == 409


async def _manager_ctx(db: Any, tenant_id: uuid.UUID, user_id: uuid.UUID) -> RequestContext:
    from app.services import directory_service

    role_id = await directory_service.role_id_by_key(db, "recruitment_manager")
    principal = Principal(
        type="user",
        id=user_id,
        tenant_id=tenant_id,
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
        role_ids=frozenset({role_id}),
    )
    return RequestContext(tenant_id=tenant_id, principal=principal)


def _admin_ctx(tenant_id: uuid.UUID, user_id: uuid.UUID) -> RequestContext:
    principal = Principal(
        type="user", id=user_id, tenant_id=tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT)
    )
    return RequestContext(tenant_id=tenant_id, principal=principal)


async def test_above_threshold_needs_approval_and_return_goes_back_to_draft(
    dorg: DOrg, outbox: Any, app: Any
) -> None:
    m = dorg.admin
    manager = await join(app, dorg.tenant, "recruitment_manager", name="Rita Manager")
    await setup_marketing(m, approval_threshold=2)
    ids = [await candidate(m, f"C{i}") for i in range(2)]
    aud = await audience(m, ids)
    camp = await email_campaign(m, aud["id"])
    resp = await submit(m, camp["id"])
    assert resp.json()["status"] == "pending_approval", resp.text
    assert await messages(dorg.tenant_id, campaign_id=camp["id"]) == []
    request_id = uuid.UUID(resp.json()["approval_request_id"])
    async with tenant_session(dorg.tenant_id) as db:
        await approvals.decide(
            await _manager_ctx(db, dorg.tenant_id, manager.user_id),
            db,
            request_id,
            DecideIn(action="approve"),
            actor=ActorRef("user", manager.user_id),
        )
    after = (await m.client.get(f"{API}/campaigns/{camp['id']}")).json()
    assert after["status"] in ("sending", "completed")
    assert len([e for e in outbox.sent if e.kind == "comms_campaign"]) == 2

    camp2 = await email_campaign(m, aud["id"], name="Second")
    resp2 = await submit(m, camp2["id"])
    async with tenant_session(dorg.tenant_id) as db:
        await approvals.decide(
            await _manager_ctx(db, dorg.tenant_id, manager.user_id), db, uuid.UUID(resp2.json()["approval_request_id"]), DecideIn(action="reject", comment="No"),
            actor=ActorRef("user", manager.user_id),
        )  # fmt: skip
    assert (await m.client.get(f"{API}/campaigns/{camp2['id']}")).json()["status"] == "draft"


async def test_unregistered_sms_template_is_refused_and_sms_goes_through_messaging(dorg: DOrg) -> None:
    m = dorg.admin
    await setup_marketing(m)
    await p2.enable(m)
    await p2.connector(m, "sms")
    transactional = await p2.template(m, "sms")
    cid = await candidate(m, "Sms", phone="+919876522222")
    aud = await audience(m, [cid])
    resp = await m.client.post(
        f"{API}/campaigns",
        json={
            "name": "SMS",
            "channel": "sms",
            "audience_id": aud["id"],
            "channel_template_id": transactional["id"],
        },
        headers=h(m),
    )
    camp = resp.json()
    refused = await submit(m, camp["id"])
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "template_not_registered"
    marketing = await p2.template(m, "sms", purpose="marketing", name="Jobs alert")
    await m.client.patch(
        f"{API}/campaigns/{camp['id']}", json={"channel_template_id": marketing["id"]}, headers=h(m)
    )
    ok = await submit(m, camp["id"])
    assert ok.status_code == 200, ok.text
    rows = await messages(dorg.tenant_id, campaign_id=camp["id"])
    assert (
        len(rows) == 1
        and rows[0].channel_message_id is not None
        and rows[0].status in ("queued", "sent", "delivered")
    )
    assert len(FakeSmsProvider.sends()) == 1


async def test_consent_withdrawn_after_queueing_is_refused_by_the_comms_gate(dorg: DOrg, outbox: Any) -> None:
    m = dorg.admin
    await setup_marketing(m)
    cid = await candidate(m, "Late")
    aud = await audience(m, [cid])
    camp = await email_campaign(m, aud["id"])
    await submit(m, camp["id"], scheduled_at="2030-01-01T09:00:00+05:30")
    async with tenant_session(dorg.tenant_id) as db:
        c = await db.get(Campaign, uuid.UUID(camp["id"]))
        assert c is not None
        c.status = "sending"
        ids = await campaign_svc._insert_messages(db, c, [cid], None)
        await sending.send_batch(db, ids)  # queued: the Email row commits with this transaction
        from app.services.privacy import consent

        await consent.withdraw_consent(RequestContext(tenant_id=dorg.tenant_id), db, cid, "marketing")
    # after commit the eager comms.send_email runs deliver(): the marketing gate refuses
    rows = await messages(dorg.tenant_id, campaign_id=camp["id"])
    assert rows[0].status == "suppressed" and rows[0].suppression_reason == "no_marketing_consent"
    assert [e for e in outbox.sent if e.kind == "comms_campaign"] == []
    async with tenant_session(dorg.tenant_id) as db:
        email = await db.get(Email, rows[0].email_message_id)
        assert email is not None and email.status == "failed" and email.error_code == "marketing_suppress"


async def test_retry_never_double_sends_and_cancel_stops_unsent(
    dorg: DOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = dorg.admin
    await setup_marketing(m)
    ids = [await candidate(m, f"R{i}") for i in range(3)]
    aud = await audience(m, ids)
    camp = await email_campaign(m, aud["id"])
    await submit(m, camp["id"], scheduled_at="2030-01-01T09:00:00+05:30")
    calls: list[Any] = []
    real = comms_service.queue_email

    async def recording(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("to"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(comms_service, "queue_email", recording)
    async with tenant_session(dorg.tenant_id) as db:
        c = await db.get(Campaign, uuid.UUID(camp["id"]))
        assert c is not None
        c.status = "sending"
        msg_ids = await campaign_svc._insert_messages(db, c, ids, None)
        again = await campaign_svc._insert_messages(db, c, ids, None)  # unique key: no duplicates
        assert again == []
    async with tenant_session(dorg.tenant_id) as db:
        await sending.send_batch(db, msg_ids[:2])
    async with tenant_session(dorg.tenant_id) as db:
        await sending.send_batch(db, msg_ids[:2])  # task retried
    async with tenant_session(dorg.tenant_id) as db:
        await sending.send_batch(db, msg_ids[:2])
    assert len(calls) == 2
    async with tenant_session(dorg.tenant_id) as db:
        out = await campaign_svc.cancel(
            _admin_ctx(dorg.tenant_id, m.user_id), db, uuid.UUID(camp["id"]), "stop"
        )
    assert out.status == "cancelled"
    async with tenant_session(dorg.tenant_id) as db:
        statuses = {
            r.candidate_id: r.status
            for r in (
                await db.execute(
                    select(CampaignMessage).where(CampaignMessage.campaign_id == uuid.UUID(camp["id"]))
                )
            ).scalars()
        }
    assert statuses[ids[2]] == "cancelled"
    async with tenant_session(dorg.tenant_id) as db:
        await sending.send_batch(db, msg_ids)
    assert len(calls) == 2


async def test_ab_split_and_winner(dorg: DOrg, outbox: Any) -> None:
    m = dorg.admin
    await setup_marketing(m)
    ids = [await candidate(m, f"Ab{i}") for i in range(12)]
    split = campaign_svc.ab_split(ids, uuid.uuid4(), 100)
    assert (
        set(split.values()) == {"A", "B"}
        and abs(list(split.values()).count("A") - list(split.values()).count("B")) <= 1
    )
    assert campaign_svc.ab_split(ids, uuid.uuid4(), 0) == {}
    bad = await m.client.post(
        f"{API}/campaigns",
        json={"name": "x", "channel": "email", "audience_id": (await audience(m, ids))["id"], "ab_test": {
            "variants": [{"key": "A", "subject": "a"}, {"key": "B", "subject": "b"}], "sample_pct": 30}},
        headers=h(m),
    )  # fmt: skip
    assert bad.status_code == 422
    aud = await audience(m, ids)
    camp = await email_campaign(
        m,
        aud["id"],
        ab_test={
            "variants": [{"key": "A", "subject": "Subject A"}, {"key": "B", "subject": "Subject B"}],
            "sample_pct": 20,
            "metric": "click",
            "window_hours": 1,
        },
    )
    await submit(m, camp["id"])
    first = await messages(dorg.tenant_id, campaign_id=camp["id"])
    assert 0 < len(first) < 12 and {r.variant_key for r in first} <= {"A", "B"}
    b_msg = next((r for r in first if r.variant_key == "B"), None)
    from datetime import timedelta

    from app.services.campaigns import engagement

    async with tenant_session(dorg.tenant_id) as db:
        if b_msg is not None:
            cm = await db.get(CampaignMessage, b_msg.id)
            assert cm is not None
            await engagement.record(db, cm, "clicked")
        c = await db.get(Campaign, uuid.UUID(camp["id"]))
        assert c is not None and c.started_at is not None
        decided = await campaign_svc.decide_ab(db, at=c.started_at + timedelta(hours=2), campaign_id=c.id)
    assert decided == 1
    out = (await m.client.get(f"{API}/campaigns/{camp['id']}")).json()
    assert out["ab_test"]["winner_key"] == ("B" if b_msg is not None else "A")
    assert len(await messages(dorg.tenant_id, campaign_id=camp["id"])) == 12
    subjects = {e.subject for e in outbox.sent if e.kind == "comms_campaign"}
    assert f"Subject {out['ab_test']['winner_key']}" in subjects


async def test_daily_cap_defers_to_next_local_midnight(dorg: DOrg) -> None:
    m = dorg.admin
    await setup_marketing(m, daily_cap={"email": 1})
    ids = [await candidate(m, f"D{i}") for i in range(2)]
    aud = await audience(m, ids)
    camp = await email_campaign(m, aud["id"])
    await submit(m, camp["id"])
    rows = await messages(dorg.tenant_id, campaign_id=camp["id"])
    assert sorted(r.status for r in rows) == ["deferred", "sent"]
    deferred = next(r for r in rows if r.status == "deferred")
    assert deferred.defer_until is not None
    from zoneinfo import ZoneInfo

    local = deferred.defer_until.astimezone(ZoneInfo("Asia/Kolkata"))
    assert (local.hour, local.minute) == (0, 0)


async def test_whatsapp_consent_grant_helper_is_used(dorg: DOrg) -> None:
    cid = await candidate(dorg.admin, "Wa2", phone="+919876533333", consents=("marketing", "whatsapp"))
    await withdraw(dorg.tenant_id, cid, "whatsapp")
    await grant(dorg.tenant_id, cid, "whatsapp")
