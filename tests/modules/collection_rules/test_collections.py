# ruff: noqa: E501
"""Epic 63 collections domain (DB): rule validation and the seeded default, the plan endpoint, the send paths
(email_messages origin agent + P2 collection activity + log), holds / takeover, quiet hours, staleness of the
balance version, the policy check at execution, timelines, cross-tenant 404, unauthenticated 401, erasure."""

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, ValidationFailed
from app.models.collection_rules import CollectionAgentLog, CollectionRule
from app.models.comms import Email
from app.models.receivables import CollectionActivity
from app.services import collection_rules_service as svc
from tests.modules.collection_rules.helpers import (
    API,
    CollSeed,
    client_rule_for,
    dispute,
    invoice,
    plan,
    rows,
    templates_of,
)
from tests.modules.demands.helpers import create_client, h


def _ctx(seed: CollSeed) -> RequestContext:
    return RequestContext(tenant_id=seed.tenant_id, actor_kind="system")


async def test_default_rule_is_seeded_once(cseed: CollSeed) -> None:
    resp = await cseed.admin.client.get(f"{API}/collections/rules")
    assert resp.status_code == 200, resp.text
    default = next(r for r in resp.json() if r["client_id"] is None)
    assert default["name"] == "Standard reminders"
    assert [(s["offset_days"], s["tone"], s["audience"], s["allow_auto"]) for s in default["steps"]] == [
        (-3, "courteous", "billing_contacts", False), (7, "friendly", "billing_contacts", False),
        (21, "firm", "billing_contacts", False), (30, "escalation", "escalation_contact", False),
    ]  # fmt: skip
    again = await cseed.admin.client.get(f"{API}/collections/rules")
    assert sum(1 for r in again.json() if r["client_id"] is None) == 1


async def test_rule_validation(cseed: CollSeed) -> None:
    templates = await templates_of(cseed)
    other_client = await create_client(cseed.admin, "Other Co")
    bad_auto = {
        "client_id": str(other_client), "name": "x",
        "steps": [{"position": 1, "offset_days": 7, "tone": "friendly", "audience": "billing_contacts", "email_template_id": templates["friendly"], "allow_auto": True}],
    }  # fmt: skip
    resp = await cseed.admin.client.post(f"{API}/collections/rules", json=bad_auto, headers=h(cseed.admin))
    assert resp.status_code == 422, resp.text
    assert "Only pre-due courteous or friendly reminders" in resp.text
    wrong_contact = {"client_id": str(other_client), "name": "x", "escalation_contact_id": str(cseed.cfo_contact_id)}
    resp = await cseed.admin.client.post(f"{API}/collections/rules", json=wrong_contact, headers=h(cseed.admin))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "escalation_contact_other_client"
    dup = await cseed.admin.client.post(f"{API}/collections/rules", json={"client_id": str(cseed.client_id), "name": "again"}, headers=h(cseed.admin))
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "rule_exists"
    from app.models.collection_rules import CollectionRuleStep

    with pytest.raises(Exception):  # noqa: B017 - the DB CHECK ``auto`` backs the validator
        async with tenant_session(cseed.tenant_id) as db:
            db.add(CollectionRuleStep(id=uuid.uuid4(), tenant_id=cseed.tenant_id, rule_id=cseed.rule_id, position=9, offset_days=5, tone="firm", audience="billing_contacts", allow_auto=True))
            await db.flush()


async def test_plan_send_paths_and_timeline(cseed: CollSeed, outbox: object) -> None:
    pre = await invoice(cseed, "50000.00", due_in_days=2)
    overdue = await invoice(cseed, "118000.00", due_in_days=-8)
    full = await invoice(cseed, "1000.00", due_in_days=-8)
    await dispute(cseed, full, "1000.00")
    p = await plan(cseed)
    by_pos = {i["position"]: i for i in p["items"]}
    assert [d["document_id"] for d in by_pos[1]["documents"]] == [str(pre)]
    assert [d["document_id"] for d in by_pos[2]["documents"]] == [str(overdue)]
    assert {e["document_id"]: e["reason"] for e in p["excluded"]}[str(full)] == "disputed"
    step2 = uuid.UUID(by_pos[2]["step_id"])
    async with tenant_session(cseed.tenant_id) as db:
        message_id, activity_id = await svc.send_templated(_ctx(cseed), db, cseed.client_id, [overdue], step2, owner_id=cseed.admin.user_id)
    email = (await rows(cseed.tenant_id, Email, Email.id == message_id))[0]
    assert email.origin == "agent" and {"entity_type": "invoice", "entity_id": str(overdue)} in email.related
    assert "118,000.00" in email.body_html and "Asha Payables" in email.body_html
    acts = await rows(cseed.tenant_id, CollectionActivity, CollectionActivity.document_id == overdue)
    assert [(a.kind, a.id) for a in acts] == [("email", activity_id)] and "Collections Agent (friendly)" in (acts[0].body or "")
    log = await rows(cseed.tenant_id, CollectionAgentLog, CollectionAgentLog.client_id == cseed.client_id)
    assert [(r.event, r.rule_step_id) for r in log] == [("reminder_sent", step2)]
    resp = await cseed.admin.client.get(f"{API}/invoices/{overdue}/agent-log")
    assert resp.status_code == 200 and resp.json()[0]["label"] == "Sent by Collections Agent"
    # the step ran: not planned again; a resend for the same step is refused
    assert all(i["position"] != 2 for i in (await plan(cseed))["items"])
    async with tenant_session(cseed.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await svc.send_templated(_ctx(cseed), db, cseed.client_id, [overdue], step2)
    assert exc.value.code == "collections_not_in_plan"
    # the disputed invoice can never be messaged
    async with tenant_session(cseed.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await svc.send_message(_ctx(cseed), db, cseed.client_id, [full], "billing_contacts", "Reminder", "Please pay.", "friendly")
    assert exc.value.code == "collections_disputed"


async def test_message_policy_at_execution_and_escalation_recipient(cseed: CollSeed, outbox: object) -> None:
    inv = await invoice(cseed, "118000.00", due_in_days=-31)
    async with tenant_session(cseed.tenant_id) as db:
        with pytest.raises(ValidationFailed) as exc:
            await svc.send_message(_ctx(cseed), db, cseed.client_id, [inv], "escalation_contact", "Overdue", "We will initiate legal action.", "escalation")
    assert exc.value.code == "dunning_policy_violation"
    async with tenant_session(cseed.tenant_id) as db:
        message_id, _ = await svc.send_message(
            _ctx(cseed), db, cseed.client_id, [inv], "escalation_contact", "Overdue invoice",
            "Could you share a payment date for the invoice below? Happy to resolve any query.", "escalation",
        )  # fmt: skip
        recipients = (await db.execute(select(Email.id).where(Email.id == message_id))).all()
    assert recipients
    from app.models.comms import EmailRecipient

    to = await rows(cseed.tenant_id, EmailRecipient, EmailRecipient.message_id == message_id)
    assert {r.entity_id for r in to} == {cseed.cfo_contact_id}


async def test_holds_takeover_release_and_staleness(cseed: CollSeed) -> None:
    inv = await invoice(cseed, "1000.00", due_in_days=-8)
    async with tenant_session(cseed.tenant_id) as db:
        before = await svc.balance_version(db, cseed.client_id)
    resp = await cseed.admin.client.post(
        f"{API}/collections/holds", json={"client_id": str(cseed.client_id), "document_kind": "invoice", "document_id": str(inv), "mode": "takeover", "reason": "Talking to them"},
        headers=h(cseed.admin),
    )  # fmt: skip
    assert resp.status_code == 201, resp.text
    hold_id = resp.json()["id"]
    p = await plan(cseed)
    assert p["items"] == [] and p["excluded"][0]["reason"] == "takeover"
    state = await cseed.admin.client.get(f"{API}/collections/clients/{cseed.client_id}/agent-state")
    assert state.json()["state"] == "taken_over"
    async with tenant_session(cseed.tenant_id) as db:
        assert await svc.balance_version(db, cseed.client_id) != before  # hold state is part of the version
        with pytest.raises(ConflictError) as exc:
            await svc.send_message(_ctx(cseed), db, cseed.client_id, [inv], "billing_contacts", "Reminder", "Please share a payment date.", "friendly")
    assert exc.value.code == "collections_agent_paused"
    resp = await cseed.admin.client.post(f"{API}/collections/holds/{hold_id}/release", headers=h(cseed.admin))
    assert resp.status_code == 200 and resp.json()["released_at"] is not None
    log = await cseed.admin.client.get(f"{API}/collections/clients/{cseed.client_id}/agent-log")
    assert [r["event"] for r in log.json()] == ["released", "takeover"]
    assert log.json()[1]["label"].startswith("Taken over by")
    async with tenant_session(cseed.tenant_id) as db:
        v1 = await svc.balance_version(db, cseed.client_id)
    await dispute(cseed, inv, "100.00")
    async with tenant_session(cseed.tenant_id) as db:
        assert await svc.balance_version(db, cseed.client_id) != v1  # a dispute / payment moves the version


async def test_client_pause_and_do_not_remind(cseed: CollSeed) -> None:
    await invoice(cseed, "1000.00", due_in_days=-8)
    resp = await cseed.admin.client.post(f"{API}/collections/holds", json={"client_id": str(cseed.client_id), "reason": "Escalated by AM"}, headers=h(cseed.admin))
    assert resp.status_code == 201
    p = await plan(cseed)
    assert p["held"] is True and p["items"] == [] and {e["reason"] for e in p["excluded"]} == {"held"}
    dup = await cseed.admin.client.post(f"{API}/collections/holds", json={"client_id": str(cseed.client_id)}, headers=h(cseed.admin))
    assert dup.status_code == 409


async def test_cross_tenant_and_unauthenticated(cseed: CollSeed, other_cseed: CollSeed, app: object) -> None:
    c = other_cseed.admin.client
    for path in (
        f"/collections/rules/{cseed.rule_id}", f"/collections/clients/{cseed.client_id}/plan",
        f"/collections/clients/{cseed.client_id}/agent-log", f"/collections/clients/{cseed.client_id}/agent-state",
    ):
        resp = await c.get(f"{API}{path}")
        assert resp.status_code == 404, (path, resp.text)
    resp = await c.post(f"{API}/collections/holds", json={"client_id": str(cseed.client_id)}, headers=h(other_cseed.admin))
    assert resp.status_code == 404
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anon:  # type: ignore[arg-type]
        assert (await anon.get(f"{API}/collections/rules")).status_code == 401


async def test_client_rule_update_and_steps(cseed: CollSeed) -> None:
    resp = await cseed.admin.client.put(f"{API}/collections/rules/{cseed.rule_id}", json={"max_reminders_per_week": 3}, headers=h(cseed.admin))
    assert resp.status_code == 200 and resp.json()["max_reminders_per_week"] == 3
    templates = await templates_of(cseed)
    resp = await cseed.admin.client.put(
        f"{API}/collections/rules/{cseed.rule_id}/steps",
        json={"steps": [{"position": 1, "offset_days": -5, "tone": "courteous", "audience": "billing_contacts", "email_template_id": templates["courteous"], "allow_auto": True}]},
        headers=h(cseed.admin),
    )  # fmt: skip
    assert resp.status_code == 200 and resp.json()["auto_steps"] == 1


async def test_contact_erasure_unlinks_escalation_contact(cseed: CollSeed) -> None:
    from app.services.collection_rules_erasure import CollectionLogHandler
    from app.services.privacy.registry import ErasureContext

    async with tenant_session(cseed.tenant_id) as db:
        ctx = ErasureContext(
            tenant_id=cseed.tenant_id, candidate_id=cseed.cfo_contact_id, run_id=uuid.uuid4(), reason="dsr",
            subject_kind="contact", subject_id=cseed.cfo_contact_id,
        )  # fmt: skip
        handler = CollectionLogHandler()
        out = await handler.erase(db, ctx)
        assert out["collection_rules_unlinked"] == 1
        assert await handler.verify(db, ctx) == []
        rule = await db.get(CollectionRule, cseed.rule_id)
        assert rule is not None and rule.escalation_contact_id is None


async def test_second_client_rule_helper(cseed: CollSeed) -> None:
    other = await create_client(cseed.admin, "Beta Co")
    seed2 = CollSeed(cseed.tenant_id, cseed.admin, cseed.entity_id, other, cseed.billing_contact_id, cseed.cfo_contact_id)
    with pytest.raises(AssertionError):  # the CFO contact belongs to another client -> 422
        await client_rule_for(seed2)
