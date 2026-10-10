"""7.2 invitations: .ics attachments parsed with ``icalendar`` -- stable UID, SEQUENCE, UTC times, the
recipient's time-zone line; candidate copy without notes; London organiser / IST candidate acceptance."""

from datetime import UTC, datetime, timedelta

from icalendar import Calendar

from app.core.mailer import OutboxMailer
from tests.modules.interviews.helpers import (
    API,
    external,
    h,
    internal,
    local,
    mails,
    plan,
    schedule,
    submitted_app,
)
from tests.modules.pipeline.helpers import POrg


def _event(msg: object) -> tuple[str, object]:
    att = msg.attachments[0]  # type: ignore[attr-defined]
    assert att.filename == "invite.ics"
    assert att.content_type.startswith("text/calendar; method=")
    cal = Calendar.from_ical(att.data)
    return str(cal.get("method")), next(iter(cal.walk("VEVENT")))


async def test_ics_request_update_cancel(porg: POrg, outbox: OutboxMailer) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (
        await schedule(
            porg.admin,
            a["application_id"],
            sid,
            [internal(porg.admin.user_id), external("ext@partner.example.com", timezone="America/New_York")],
            tz="Europe/London",
            candidate_timezone="Asia/Kolkata",
            notes="Probe Spark internals",
        )
    ).json()
    starts = datetime.fromisoformat(body["starts_at"].replace("Z", "+00:00"))
    invites = mails(outbox, "interview_invite")
    assert len(invites) == 3
    uids = set()
    for m in invites:
        method, ev = _event(m)
        assert method == "REQUEST"
        assert int(ev.get("sequence")) == 0
        assert ev.decoded("dtstart") == starts.astimezone(UTC)
        assert ev.decoded("dtstart").tzinfo is not None and ev.decoded("dtstart").utcoffset() == timedelta(0)
        assert "DTSTART:" in m.attachments[0].data.decode() and "Z\r\n" in m.attachments[0].data.decode()
        uids.add(str(ev.get("uid")))
    assert len(uids) == 1
    candidate = mails(outbox, "interview_invite", to=a["candidate_email"].lower())[0]
    _, ev = _event(candidate)
    assert "Time zone for this invitation: Asia/Kolkata" in str(ev.get("description"))
    assert "IST (UTC+05:30)" in candidate.text and "Asia/Kolkata" in candidate.text
    assert "Probe Spark internals" not in candidate.text and "Probe Spark" not in str(ev.get("description"))
    assert candidate.subject.endswith("interview for Senior Data Engineer")
    ext = mails(outbox, "interview_invite", to="ext@partner.example.com")[0]
    _, ev = _event(ext)
    assert "Time zone for this invitation: America/New_York" in str(ev.get("description"))
    assert ext.subject.startswith("Interview invitation: Priya")
    assert "/l/" in ext.text  # scorecard secure link
    organiser = mails(outbox, "interview_invite", to=porg.admin.email.lower())[0]
    assert "Probe Spark internals" in organiser.text
    assert "Time zone for this invitation: Asia/Kolkata" in str(_event(candidate)[1].get("description"))

    outbox.sent.clear()
    resp = await porg.admin.client.patch(
        f"{API}/interviews/{body['id']}/reschedule",
        json={
            "start_local": local("Europe/London", timedelta(days=5)),
            "timezone": "Europe/London",
            "expected_version": body["version"],
        },
        headers=h(porg.admin),
    )
    assert resp.status_code == 200, resp.text
    for m in mails(outbox, "interview_invite"):
        method, ev = _event(m)
        assert method == "REQUEST" and int(ev.get("sequence")) == 1 and str(ev.get("uid")) in uids

    outbox.sent.clear()
    await porg.admin.client.post(
        f"{API}/interviews/{body['id']}/cancel", json={"reason": "Role on hold"}, headers=h(porg.admin)
    )
    for m in mails(outbox, "interview_cancel"):
        method, ev = _event(m)
        assert method == "CANCEL" and str(ev.get("status")) == "CANCELLED" and int(ev.get("sequence")) == 2
        assert str(ev.get("uid")) in uids


async def test_invite_task_is_idempotent(porg: POrg, outbox: OutboxMailer) -> None:
    import uuid

    from app.core.database import tenant_session
    from app.services import interviews_service

    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (await schedule(porg.admin, a["application_id"], sid, [internal(porg.admin.user_id)])).json()
    assert len(mails(outbox, "interview_invite")) == 2
    async with tenant_session(porg.tenant_id) as db:
        again = await interviews_service.sync_invites(db, uuid.UUID(body["id"]), "request")
    assert again == {"sent": 0}
    assert len(mails(outbox, "interview_invite")) == 2
