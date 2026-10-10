# ruff: noqa: E501
"""Epic 39 story 39.5 (FR-199): nurture sequences, versions, enrolment rules, the advance beat, exits."""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from app.core.database import tenant_session
from app.models.campaigns import CampaignMessage, Enrollment
from app.services.campaigns import engagement, sequences
from tests.epic5 import h
from tests.modules.campaigns.helpers import API, candidate, now, setup_marketing, withdraw
from tests.modules.demands.helpers import DOrg


def _send(subject: str = "Hi {{candidate.first_name}}") -> dict[str, Any]:
    return {"kind": "send", "config": {"channel": "email", "subject": subject, "body_html": "<p>New roles</p>"}}


def _wait(days: int) -> dict[str, Any]:
    return {"kind": "wait", "config": {"days": days}}


async def _sequence(m: Any, steps: list[dict[str, Any]], **body: Any) -> dict[str, Any]:
    resp = await m.client.post(f"{API}/sequences", json={"name": "Nurture", "steps": steps, **body}, headers=h(m))
    assert resp.status_code == 201, resp.text
    pub = await m.client.post(f"{API}/sequences/{resp.json()['id']}/publish", json={"note": "v1"}, headers=h(m))
    assert pub.status_code == 200, pub.text
    return dict(pub.json())


async def _enrol(m: Any, seq_id: str, *cids: uuid.UUID) -> Any:
    return await m.client.post(f"{API}/sequences/{seq_id}/enrol", json={"candidate_ids": [str(c) for c in cids]}, headers=h(m))


async def _advance(tenant_id: uuid.UUID, at: Any = None) -> int:
    async with tenant_session(tenant_id) as db:
        return await sequences.advance(db, at=at)


async def _enrollment(tenant_id: uuid.UUID, enrollment_id: Any) -> Enrollment:
    async with tenant_session(tenant_id) as db:
        e = await db.get(Enrollment, uuid.UUID(str(enrollment_id)))
        assert e is not None
        return e


async def test_step_graph_validation(dorg: DOrg) -> None:
    m = dorg.admin
    backward = [_send(), {"kind": "branch", "config": {"on": "clicked", "within_days": 3, "then_position": 1, "else_position": 3}}, _send()]
    resp = await m.client.post(f"{API}/sequences", json={"name": "Loop", "steps": backward}, headers=h(m))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_sequence"
    too_long = await m.client.post(f"{API}/sequences", json={"name": "Long", "steps": [_wait(1)] * 21}, headers=h(m))
    assert too_long.status_code == 422
    bad_wait = await m.client.post(f"{API}/sequences", json={"name": "W", "steps": [_wait(91)]}, headers=h(m))
    assert bad_wait.status_code == 422
    empty = await m.client.post(f"{API}/sequences", json={"name": "Empty", "steps": []}, headers=h(m))
    publish = await m.client.post(f"{API}/sequences/{empty.json()['id']}/publish", headers=h(m))
    assert publish.status_code == 422
    not_active = await _enrol(m, empty.json()["id"], uuid.uuid4())
    assert not_active.status_code == 409 and not_active.json()["error"]["code"] == "sequence_not_active"


async def test_four_step_sequence_exits_on_reply_after_step_two(dorg: DOrg, outbox: Any) -> None:
    m = dorg.admin
    await setup_marketing(m)
    cid = await candidate(m, "Ravi")
    seq = await _sequence(m, [_send("Step one"), _wait(2), _send("Step three"), _wait(3)])
    resp = await _enrol(m, seq["id"], cid)
    assert resp.status_code == 200 and resp.json()["enrolled"] == 1, resp.text
    eid = resp.json()["enrollment_ids"][0]
    await _advance(dorg.tenant_id)
    e = await _enrollment(dorg.tenant_id, eid)
    assert e.position == 3 and e.status == "active" and e.next_run_at is not None and e.next_run_at > now()
    assert [x.subject for x in outbox.sent if x.kind in ("comms_campaign", "comms_user")] == ["Step one"]
    # the candidate replies (P2 inbound / reply capture) -> exited(replied), owner notified
    async with tenant_session(dorg.tenant_id) as db:
        assert await engagement.on_reply(db, cid, "email", "reply-1")
    e = await _enrollment(dorg.tenant_id, eid)
    assert (e.status, e.exit_reason) == ("exited", "replied")
    await _advance(dorg.tenant_id, now() + timedelta(days=3))
    assert len([x for x in outbox.sent if x.kind in ("comms_campaign", "comms_user")]) == 1
    listed = (await m.client.get(f"{API}/sequences/{seq['id']}/enrollments")).json()
    assert listed[0]["exit_reason"] == "replied"
    stats = (await m.client.get(f"{API}/sequences/{seq['id']}/analytics")).json()
    assert stats["counts"]["replied"] == 1 and stats["steps"][0]["key"] == "1"


async def test_one_active_enrolment_and_thirty_day_re_enrol_gap(dorg: DOrg) -> None:
    m = dorg.admin
    await setup_marketing(m)
    cid = await candidate(m, "Gap")
    seq = await _sequence(m, [_wait(5)])
    first = await _enrol(m, seq["id"], cid)
    assert first.json()["enrolled"] == 1
    again = await _enrol(m, seq["id"], cid)
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_enrolled"
    eid = first.json()["enrollment_ids"][0]
    out = await m.client.post(f"{API}/enrollments/{eid}/exit", headers=h(m))
    assert out.json()["status"] == "exited" and out.json()["exit_reason"] == "manual"
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(update(Enrollment).where(Enrollment.id == uuid.UUID(eid)).values(ended_at=now() - timedelta(days=10)))
    gap = await _enrol(m, seq["id"], cid)
    assert gap.status_code == 409 and gap.json()["error"]["code"] == "re_enrol_gap"
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(update(Enrollment).where(Enrollment.id == uuid.UUID(eid)).values(ended_at=now() - timedelta(days=31)))
    assert (await _enrol(m, seq["id"], cid)).json()["enrolled"] == 1


async def test_version_pinning_after_publish(dorg: DOrg, outbox: Any) -> None:
    m = dorg.admin
    await setup_marketing(m)
    a = await candidate(m, "Pinned")
    seq = await _sequence(m, [_wait(1), _send("Version one")])
    enrolled = (await _enrol(m, seq["id"], a)).json()["enrollment_ids"][0]
    edit = await m.client.patch(f"{API}/sequences/{seq['id']}", json={"steps": [_wait(1), _send("Version two")]}, headers=h(m))
    assert edit.status_code == 200 and edit.json()["current_version"] == 1
    pub = await m.client.post(f"{API}/sequences/{seq['id']}/publish", headers=h(m))
    assert pub.json()["current_version"] == 2
    b = await candidate(m, "Fresh")
    (await _enrol(m, seq["id"], b)).json()
    await _advance(dorg.tenant_id)
    await _advance(dorg.tenant_id, now() + timedelta(days=2))
    assert (await _enrollment(dorg.tenant_id, enrolled)).version == 1
    subjects = sorted(x.subject for x in outbox.sent if x.kind in ("comms_campaign", "comms_user"))
    assert subjects == ["Version one", "Version two"]


async def test_branch_on_click_and_suppression_ends_the_enrolment(dorg: DOrg, outbox: Any) -> None:
    m = dorg.admin
    await setup_marketing(m)
    clicker = await candidate(m, "Clicker")
    quiet = await candidate(m, "Quiet")
    steps = [
        _send("Intro"),
        {"kind": "branch", "config": {"on": "clicked", "within_days": 7, "then_position": 3, "else_position": 4}},
        _send("Thanks for clicking"),
        _wait(10),
    ]
    seq = await _sequence(m, steps)
    ids = (await _enrol(m, seq["id"], clicker, quiet)).json()["enrollment_ids"]
    # intro goes out, branch evaluated in the same run -> both took the else path (no click yet)
    await _advance(dorg.tenant_id)
    assert {(await _enrollment(dorg.tenant_id, i)).position for i in ids} == {5}
    # a new sequence where the click happens between the send and the branch (wait in between)
    steps2 = [_send("Intro 2"), _wait(1), steps[1] | {"config": {**steps[1]["config"], "then_position": 4, "else_position": 5}}, _send("Clicked path"), _wait(10)]
    seq2 = await _sequence(m, steps2)
    ids2 = (await _enrol(m, seq2["id"], clicker)).json()["enrollment_ids"]
    await _advance(dorg.tenant_id)
    async with tenant_session(dorg.tenant_id) as db:
        cm = (await db.execute(select(CampaignMessage).where(CampaignMessage.enrollment_id == uuid.UUID(ids2[0])))).scalar_one()
        await engagement.record(db, cm, "clicked")
    await _advance(dorg.tenant_id, now() + timedelta(days=2))
    assert "Clicked path" in {x.subject for x in outbox.sent}
    # suppression (consent withdrawn) ends an enrolment as suppressed
    s3 = await _sequence(m, [_wait(1), _send("Never")])
    victim = await candidate(m, "Victim")
    eid = (await _enrol(m, s3["id"], victim)).json()["enrollment_ids"][0]
    await withdraw(dorg.tenant_id, victim, "marketing")
    await _advance(dorg.tenant_id)
    await _advance(dorg.tenant_id, now() + timedelta(days=2))
    e = await _enrollment(dorg.tenant_id, eid)
    assert e.status == "suppressed"
    assert "Never" not in {x.subject for x in outbox.sent}
