"""7.2 scheduling: Submitted -> Interviewing on the first interview (actor system), stage checks, reschedule
(SEQUENCE + 1, same UID), cancel (CANCEL to everyone), version conflict, participants, cross-tenant 404."""

import uuid
from datetime import timedelta

from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.mailer import OutboxMailer
from app.models.interviews import Interview
from app.models.pipeline import ApplicationStageHistory, PipelineStage
from tests.modules.identity.conftest import join
from tests.modules.interviews.helpers import (
    API,
    external,
    h,
    internal,
    local,
    mails,
    plan,
    schedule,
    stage_key,
    submitted_app,
)
from tests.modules.pipeline.helpers import POrg, add_one, create_candidate, shortlist


async def test_first_interview_moves_to_interviewing_with_system_actor(
    porg: POrg, outbox: OutboxMailer
) -> None:
    a = await submitted_app(porg)
    stages = await plan(porg.admin, porg.demand_id)
    assert [s["name"] for s in stages] == ["Technical", "Client", "HR"]
    assert [s["is_final"] for s in stages] == [False, False, True]
    resp = await schedule(porg.admin, a["application_id"], stages[0]["id"], [internal(porg.admin.user_id)])
    body = resp.json()
    assert body["status"] == "scheduled" and body["sequence"] == 0 and body["human_id"].startswith("INT-")
    assert await stage_key(porg.admin, a["application_id"]) == "interviewing"
    async with tenant_session(porg.tenant_id) as db:
        history = (
            await db.execute(
                select(ApplicationStageHistory, PipelineStage.key)
                .join(PipelineStage, PipelineStage.id == ApplicationStageHistory.to_stage_id)
                .where(ApplicationStageHistory.application_id == uuid.UUID(a["application_id"]))
                .order_by(ApplicationStageHistory.at)
            )
        ).all()
    last, key = history[-1]
    assert key == "interviewing" and last.actor_kind == "system"
    # A second interview does not move the application again.
    await schedule(
        porg.admin,
        a["application_id"],
        stages[1]["id"],
        [internal(porg.admin.user_id)],
        start=timedelta(days=5),
    )
    async with tenant_session(porg.tenant_id) as db:
        count = len(
            (
                await db.execute(
                    select(ApplicationStageHistory).where(
                        ApplicationStageHistory.application_id == uuid.UUID(a["application_id"])
                    )
                )
            ).all()
        )
    assert count == len(history)
    demand = (await porg.admin.client.get(f"{API}/demands/{porg.demand_id}")).json()
    assert demand["interviews_scheduled_count"] == 2


async def test_not_interviewable_and_wrong_stage(porg: POrg, other_porg: POrg) -> None:
    cand = await create_candidate(porg.admin)
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    await shortlist(porg.admin, app_id)
    stages = await plan(porg.admin, porg.demand_id)
    resp = await schedule(porg.admin, app_id, stages[0]["id"], [internal(porg.admin.user_id)], expect=409)
    assert resp.json()["error"]["code"] == "application_not_interviewable"

    a = await submitted_app(porg, "Rahul Sharma")
    other_stages = await plan(other_porg.admin, other_porg.demand_id)
    resp = await schedule(
        porg.admin, a["application_id"], other_stages[0]["id"], [internal(porg.admin.user_id)], expect=409
    )
    assert resp.json()["error"]["code"] == "stage_not_in_plan"


async def test_validation_rules(porg: POrg) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    app_id = a["application_id"]
    r = await schedule(
        porg.admin,
        app_id,
        sid,
        [internal(porg.admin.user_id)],
        tz="Mars/Base",
        start_local="2026-12-01T10:00",
        expect=422,
    )
    assert r.json()["error"]["code"] == "invalid_timezone"
    r = await schedule(
        porg.admin,
        app_id,
        sid,
        [internal(porg.admin.user_id)],
        tz="America/New_York",
        start_local="2027-03-14T02:30",
        expect=422,
    )
    assert r.json()["error"]["code"] == "nonexistent_local_time"
    r = await schedule(porg.admin, app_id, sid, [], expect=422)
    assert r.json()["error"]["code"] == "interviewer_required"
    r = await schedule(
        porg.admin, app_id, sid, [internal(porg.admin.user_id)], duration_minutes=10, expect=422
    )
    assert r.json()["error"]["code"] == "invalid_duration"
    r = await schedule(
        porg.admin, app_id, sid, [internal(porg.admin.user_id)], start=timedelta(hours=-2), expect=422
    )
    assert r.json()["error"]["code"] == "start_in_past"
    # Backfill: allowed with allow_past, no invitations.
    await schedule(
        porg.admin, app_id, sid, [internal(porg.admin.user_id)], start=timedelta(hours=-2), allow_past=True
    )


async def test_candidate_email_missing(porg: POrg) -> None:
    cand = await create_candidate(porg.admin, "No Mail", emails=[])
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    await shortlist(porg.admin, app_id)
    from app.services import pipeline_service
    from tests.modules.interviews.helpers import admin_ctx

    async with tenant_session(porg.tenant_id) as db:
        await pipeline_service.transition_application(
            admin_ctx(porg), db, uuid.UUID(app_id), "submitted", "system"
        )
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    r = await schedule(porg.admin, app_id, sid, [internal(porg.admin.user_id)], expect=409)
    assert r.json()["error"]["code"] == "candidate_email_missing"
    await schedule(porg.admin, app_id, sid, [internal(porg.admin.user_id)], send_invites=False)


async def test_reschedule_bumps_sequence_and_cancel_sends_cancel(porg: POrg, outbox: OutboxMailer) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (
        await schedule(
            porg.admin,
            a["application_id"],
            sid,
            [internal(porg.admin.user_id), external("ext@partner.example.com")],
        )
    ).json()
    iid = body["id"]
    assert len(mails(outbox, "interview_invite")) == 3
    async with tenant_session(porg.tenant_id) as db:
        uid = (await db.get(Interview, uuid.UUID(iid))).ical_uid  # type: ignore[union-attr]

    outbox.sent.clear()
    resp = await porg.admin.client.patch(
        f"{API}/interviews/{iid}/reschedule",
        json={
            "start_local": local("Asia/Kolkata", timedelta(days=4)),
            "timezone": "Asia/Kolkata",
            "duration_minutes": 45,
            "expected_version": body["version"],
        },
        headers=h(porg.admin),
    )
    assert resp.status_code == 200, resp.text
    moved = resp.json()
    assert (
        moved["sequence"] == 1 and moved["duration_minutes"] == 45 and moved["version"] == body["version"] + 1
    )
    invites = mails(outbox, "interview_invite")
    assert len(invites) == 3 and all(m.subject.startswith("Updated:") for m in invites)
    async with tenant_session(porg.tenant_id) as db:
        assert (await db.get(Interview, uuid.UUID(iid))).ical_uid == uid  # type: ignore[union-attr]

    stale = await porg.admin.client.patch(
        f"{API}/interviews/{iid}/reschedule",
        json={
            "start_local": local("UTC", timedelta(days=6)),
            "timezone": "UTC",
            "expected_version": body["version"],
        },
        headers=h(porg.admin),
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"

    outbox.sent.clear()
    resp = await porg.admin.client.post(
        f"{API}/interviews/{iid}/cancel", json={"reason": "Client paused"}, headers=h(porg.admin)
    )
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled" and resp.json()["sequence"] == 2
    cancels = mails(outbox, "interview_cancel")
    assert len(cancels) == 3
    assert all(b"METHOD:CANCEL" in m.attachments[0].data for m in cancels)
    again = await porg.admin.client.post(
        f"{API}/interviews/{iid}/cancel", json={"reason": "x"}, headers=h(porg.admin)
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "interview_not_scheduled"


async def test_reschedule_removes_participant_with_cancel(porg: POrg, outbox: OutboxMailer) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (
        await schedule(
            porg.admin,
            a["application_id"],
            sid,
            [internal(porg.admin.user_id), external("gone@partner.example.com")],
        )
    ).json()
    outbox.sent.clear()
    resp = await porg.admin.client.patch(
        f"{API}/interviews/{body['id']}/reschedule",
        json={
            "start_local": local("Europe/London", timedelta(days=6)),
            "timezone": "Europe/London",
            "expected_version": body["version"],
            "participants": [internal(porg.admin.user_id)],
        },
        headers=h(porg.admin),
    )
    assert resp.status_code == 200, resp.text
    assert [p["email"] for p in resp.json()["participants"] if p["kind"] == "interviewer_external"] == []
    gone = mails(outbox, to="gone@partner.example.com")
    assert len(gone) == 1 and gone[0].kind == "interview_cancel"


async def test_lists_and_visibility(app: FastAPI, porg: POrg, other_porg: POrg) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    interviewer = await join(app, porg.tenant, "interviewer", name="Ivy Interviewer")
    try:
        body = (await schedule(porg.admin, a["application_id"], sid, [internal(interviewer.user_id)])).json()
        mine = await interviewer.client.get(f"{API}/me/interviews")
        assert mine.status_code == 200 and [i["id"] for i in mine.json()["items"]] == [body["id"]]
        got = await interviewer.client.get(f"{API}/interviews/{body['id']}")
        assert got.status_code == 200
        demand_list = await porg.admin.client.get(
            f"{API}/demands/{porg.demand_id}/interviews", params={"status": "scheduled"}
        )
        assert demand_list.status_code == 200 and demand_list.json()["total"] == 1
        app_list = await porg.admin.client.get(f"{API}/applications/{a['application_id']}/interviews")
        assert app_list.status_code == 200 and len(app_list.json()) == 1
        ics = await porg.admin.client.get(f"{API}/interviews/{body['id']}/ics")
        assert ics.status_code == 200 and b"BEGIN:VCALENDAR" in ics.content
        # Interviewer role cannot schedule.
        denied = await interviewer.client.post(
            f"{API}/interviews/{body['id']}/cancel", json={"reason": "x"}, headers=h(interviewer)
        )
        assert denied.status_code == 403
    finally:
        await interviewer.client.aclose()

    o = other_porg.admin
    iid = body["id"]
    for method, url, payload in (
        ("GET", f"{API}/interviews/{iid}", None),
        ("GET", f"{API}/interviews/{iid}/ics", None),
        (
            "PATCH",
            f"{API}/interviews/{iid}/reschedule",
            {"start_local": "2030-01-01T10:00", "timezone": "UTC", "expected_version": 1},
        ),
        ("POST", f"{API}/interviews/{iid}/cancel", {"reason": "x"}),
        ("POST", f"{API}/interviews/{iid}/no-show", None),
        ("POST", f"{API}/interviews/{iid}/complete", None),
        ("GET", f"{API}/interviews/{iid}/scorecards", None),
        ("GET", f"{API}/interviews/{iid}/scorecards/mine", None),
        ("PUT", f"{API}/interviews/{iid}/scorecards/mine", {}),
        ("POST", f"{API}/interviews/{iid}/scorecards/mine/submit", None),
        ("GET", f"{API}/applications/{a['application_id']}/interviews", None),
        (
            "POST",
            f"{API}/applications/{a['application_id']}/interviews",
            {
                "stage_id": sid,
                "start_local": "2030-01-01T10:00",
                "timezone": "UTC",
                "participants": [internal(o.user_id)],
            },
        ),
        ("GET", f"{API}/applications/{a['application_id']}/interview-summary", None),
        (
            "POST",
            f"{API}/applications/{a['application_id']}/stage-outcomes",
            {"stage_id": sid, "outcome": "hold"},
        ),
        ("POST", f"{API}/applications/{a['application_id']}/select", None),
        ("GET", f"{API}/demands/{porg.demand_id}/interviews", None),
        ("GET", f"{API}/demands/{porg.demand_id}/interview-plan", None),
        ("PUT", f"{API}/demands/{porg.demand_id}/interview-plan", {"stages": []}),
    ):
        resp = await o.client.request(method, url, json=payload, headers=h(o))
        assert resp.status_code == 404, (method, url, resp.status_code, resp.text)
