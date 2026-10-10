"""7.3 scorecards: blind rule, submit before start 409, locked after submit, external secure-link flow (view,
not-started, validation, single use 410, expiry at ends_at + 7 d), missing-scorecard reminder once."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.core import secure_links
from app.core.database import tenant_session
from app.core.mailer import OutboxMailer
from app.services import interviews_service
from tests.modules.identity.conftest import join
from tests.modules.interviews.helpers import (
    API,
    Clock,
    external,
    h,
    internal,
    links_for,
    mails,
    plan,
    schedule,
    submitted_app,
)
from tests.modules.pipeline.helpers import POrg

GOOD = {
    "ratings": [{"competency_key": "overall", "rating": 4, "note": "Solid"}],
    "recommendation": "yes",
    "summary": "Hire",
}


def _at(body: dict[str, str], key: str) -> datetime:
    return datetime.fromisoformat(body[key].replace("Z", "+00:00"))


async def test_blind_rule_two_interviewers(app: FastAPI, porg: POrg, clock: Clock) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    ann = await join(app, porg.tenant, "interviewer", name="Ann Interviewer")
    bob = await join(app, porg.tenant, "interviewer", name="Bob Interviewer")
    try:
        body = (
            await schedule(
                porg.admin, a["application_id"], sid, [internal(ann.user_id), internal(bob.user_id)]
            )
        ).json()
        iid = body["id"]
        early = await ann.client.post(
            f"{API}/interviews/{iid}/scorecards/mine/submit", json=GOOD, headers=h(ann)
        )
        assert early.status_code == 409 and early.json()["error"]["code"] == "interview_not_started"

        clock.set(_at(body, "starts_at") + timedelta(minutes=30))
        draft = await ann.client.put(
            f"{API}/interviews/{iid}/scorecards/mine",
            json={"ratings": [{"competency_key": "overall", "rating": 5}]},
            headers=h(ann),
        )
        assert draft.status_code == 200 and draft.json()["status"] == "draft"
        incomplete = await ann.client.post(f"{API}/interviews/{iid}/scorecards/mine/submit", headers=h(ann))
        assert incomplete.status_code == 422 and incomplete.json()["error"]["code"] == "scorecard_incomplete"
        out_of_range = await ann.client.post(
            f"{API}/interviews/{iid}/scorecards/mine/submit",
            json={**GOOD, "ratings": [{"competency_key": "overall", "rating": 9}]},
            headers=h(ann),
        )
        assert out_of_range.status_code == 422
        done = await ann.client.post(
            f"{API}/interviews/{iid}/scorecards/mine/submit", json=GOOD, headers=h(ann)
        )
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "submitted"
        locked = await ann.client.put(f"{API}/interviews/{iid}/scorecards/mine", json=GOOD, headers=h(ann))
        assert locked.status_code == 409 and locked.json()["error"]["code"] == "scorecard_locked"

        # Bob has not submitted: hidden with counts.
        hidden = (await bob.client.get(f"{API}/interviews/{iid}/scorecards")).json()
        assert hidden["hidden"] is True and hidden["submitted_count"] == 1 and hidden["required_count"] == 2
        assert hidden["items"] == []
        # Ann submitted: she sees the submitted cards.
        seen = (await ann.client.get(f"{API}/interviews/{iid}/scorecards")).json()
        assert seen["hidden"] is False and len(seen["items"]) == 1
        # The recruiter (outcome permission, not a required participant) sees everything.
        recruiter = (await porg.admin.client.get(f"{API}/interviews/{iid}/scorecards")).json()
        assert recruiter["hidden"] is False and recruiter["items"][0]["recommendation"] == "yes"
        # Application aggregate follows the same rule.
        summary_bob = await bob.client.get(f"{API}/applications/{a['application_id']}/interview-summary")
        assert summary_bob.status_code == 404  # interviewers cannot read the application itself
        summary = (
            await porg.admin.client.get(f"{API}/applications/{a['application_id']}/interview-summary")
        ).json()
        tech = summary["stages"][0]
        assert (
            tech["hidden"] is False
            and tech["recommendations"]["yes"] == 1
            and tech["scorecards_submitted"] == 1
        )

        bob_done = await bob.client.post(
            f"{API}/interviews/{iid}/scorecards/mine/submit", json=GOOD, headers=h(bob)
        )
        assert bob_done.status_code == 200
        assert (await bob.client.get(f"{API}/interviews/{iid}/scorecards")).json()["hidden"] is False
    finally:
        await ann.client.aclose()
        await bob.client.aclose()


async def test_recruiter_who_must_score_is_blinded(porg: POrg, clock: Clock) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (
        await schedule(
            porg.admin,
            a["application_id"],
            sid,
            [internal(porg.admin.user_id), external("x@partner.example.com")],
        )
    ).json()
    listing = (await porg.admin.client.get(f"{API}/interviews/{body['id']}/scorecards")).json()
    assert listing["hidden"] is True and listing["required_count"] == 2
    _ = clock


async def test_external_link_flow(
    porg: POrg,
    other_porg: POrg,
    outbox: OutboxMailer,
    public: httpx.AsyncClient,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (
        await schedule(
            porg.admin,
            a["application_id"],
            sid,
            [external("eli@partner.example.com", timezone="America/New_York")],
        )
    ).json()
    tokens = links_for(outbox, "eli@partner.example.com")
    assert len(tokens) == 1
    token = tokens[0]

    view = await public.get(f"{API}/public/links/{token}")
    assert view.status_code == 200, view.text
    v = view.json()
    assert v["purpose"] == "interview_scorecard" and v["scorecard"]["state"] == "not_started"
    assert v["scorecard"]["candidate_display_name"] == "Priya R."
    assert v["scorecard"]["timezone"] == "America/New_York" and "(UTC-0" in v["scorecard"]["display_time"]
    assert v["scorecard"]["competencies"][0]["key"] == "overall"
    assert view.headers["cache-control"] == "no-store"

    early = await public.post(f"{API}/public/links/{token}/scorecard", json=GOOD)
    assert early.status_code == 409 and early.json()["error"]["code"] == "interview_not_started"

    clock.set(_at(body, "ends_at") + timedelta(hours=1))
    assert (await public.get(f"{API}/public/links/{token}")).json()["scorecard"]["state"] == "open"
    bad = await public.post(f"{API}/public/links/{token}/scorecard", json={**GOOD, "ratings": []})
    assert bad.status_code == 422  # validation does not consume the link
    ok = await public.post(f"{API}/public/links/{token}/scorecard", json=GOOD)
    assert ok.status_code == 200 and ok.json()["status"] == "submitted"
    reuse = await public.post(f"{API}/public/links/{token}/scorecard", json=GOOD)
    assert reuse.status_code == 410 and reuse.json()["error"]["code"] == "link_used"

    listing = (await porg.admin.client.get(f"{API}/interviews/{body['id']}/scorecards")).json()
    assert listing["items"][0]["source"] == "secure_link"

    # A second interview: the link expires at ends_at + 7 days.
    b = (
        await schedule(
            porg.admin,
            a["application_id"],
            sid,
            [external("zed@partner.example.com")],
            start=timedelta(days=5),
        )
    ).json()
    token2 = links_for(outbox, "zed@partner.example.com")[0]
    async with tenant_session(porg.tenant_id) as db:
        link = await db.get(secure_links.SecureLink, (await _participant_link(db, b["id"])))
    assert link is not None
    assert abs((link.expires_at - (_at(b, "ends_at") + timedelta(days=7))).total_seconds()) < 5
    monkeypatch.setattr(secure_links, "_now", lambda: _at(b, "ends_at") + timedelta(days=7, minutes=1))
    expired = await public.get(f"{API}/public/links/{token2}")
    assert expired.status_code == 410 and expired.json()["error"]["code"] == "link_expired"
    monkeypatch.undo()

    # Tenant B's link does not resolve from anywhere else; a random token is 404.
    assert (await public.get(f"{API}/public/links/{'x' * 43}")).status_code == 404
    _ = other_porg


async def _participant_link(db: object, interview_id: str) -> uuid.UUID:
    from sqlalchemy import select

    from app.models.interviews import InterviewParticipant

    row = (
        await db.execute(  # type: ignore[attr-defined]
            select(InterviewParticipant.secure_link_id).where(
                InterviewParticipant.interview_id == uuid.UUID(interview_id),
                InterviewParticipant.kind == "interviewer_external",
            )
        )
    ).scalar_one()
    return row


async def test_cancel_revokes_external_link(
    porg: POrg, outbox: OutboxMailer, public: httpx.AsyncClient
) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (
        await schedule(porg.admin, a["application_id"], sid, [external("eli@partner.example.com")])
    ).json()
    token = links_for(outbox, "eli@partner.example.com")[0]
    await porg.admin.client.post(
        f"{API}/interviews/{body['id']}/cancel", json={"reason": "Paused"}, headers=h(porg.admin)
    )
    resp = await public.get(f"{API}/public/links/{token}")
    assert resp.status_code == 410 and resp.json()["error"]["code"] == "link_revoked"


async def test_missing_scorecard_reminder_once(
    app: FastAPI, porg: POrg, outbox: OutboxMailer, clock: Clock
) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    ann = await join(app, porg.tenant, "interviewer", name="Ann Interviewer")
    try:
        body = (
            await schedule(
                porg.admin,
                a["application_id"],
                sid,
                [internal(ann.user_id), external("eli@partner.example.com")],
            )
        ).json()
        ends = _at(body, "ends_at")
        clock.set(ends + timedelta(minutes=5))
        async with tenant_session(porg.tenant_id) as db:
            assert await interviews_service.complete_elapsed(db) == 1
        clock.set(ends + timedelta(hours=23))
        async with tenant_session(porg.tenant_id) as db:
            assert await interviews_service.scorecard_reminders(db) == 0
        outbox.sent.clear()
        clock.set(ends + timedelta(hours=25))
        async with tenant_session(porg.tenant_id) as db:
            assert await interviews_service.scorecard_reminders(db) == 2
        reminders = mails(outbox, "scorecard_reminder")
        assert {m.to for m in reminders} == {ann.email.lower(), "eli@partner.example.com"}
        assert "/interviews/" in next(m for m in reminders if m.to == ann.email.lower()).text
        assert "/l/" in next(m for m in reminders if m.to == "eli@partner.example.com").text
        async with tenant_session(porg.tenant_id) as db:
            assert await interviews_service.scorecard_reminders(db) == 0
    finally:
        await ann.client.aclose()
    _ = UTC
