"""7.2 reminders (AD-9): 24 h and 1 h sent once each (per-recipient time zone); concurrent runs send once;
offsets already passed at scheduling time are skipped; rescheduling resets them."""

import asyncio
from datetime import datetime, timedelta

from app.core.database import tenant_session
from app.core.mailer import OutboxMailer
from app.services import interviews_service
from tests.modules.interviews.helpers import (
    API,
    Clock,
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


def _at(body: dict[str, str], key: str) -> datetime:
    return datetime.fromisoformat(body[key].replace("Z", "+00:00"))


async def _run(porg: POrg) -> int:
    async with tenant_session(porg.tenant_id) as db:
        return await interviews_service.send_due_reminders(db)


async def test_reminders_sent_once_each(porg: POrg, outbox: OutboxMailer, clock: Clock) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (
        await schedule(
            porg.admin,
            a["application_id"],
            sid,
            [internal(porg.admin.user_id), external("ext@partner.example.com", timezone="America/New_York")],
            candidate_timezone="Asia/Kolkata",
        )
    ).json()
    starts = _at(body, "starts_at")
    outbox.sent.clear()
    clock.set(starts - timedelta(hours=30))
    assert await _run(porg) == 0
    clock.set(starts - timedelta(hours=23, minutes=55))
    assert await _run(porg) == 3
    assert await _run(porg) == 0
    candidate = mails(outbox, "interview_reminder", to=a["candidate_email"].lower())
    assert len(candidate) == 1 and "IST (UTC+05:30)" in candidate[0].text
    ext = mails(outbox, "interview_reminder", to="ext@partner.example.com")
    assert "America/New_York" in ext[0].text
    clock.set(starts - timedelta(minutes=50))
    assert await _run(porg) == 3
    assert await _run(porg) == 0
    assert len(mails(outbox, "interview_reminder")) == 6


async def test_concurrent_runs_send_once(porg: POrg, outbox: OutboxMailer, clock: Clock) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (await schedule(porg.admin, a["application_id"], sid, [internal(porg.admin.user_id)])).json()
    outbox.sent.clear()
    clock.set(_at(body, "starts_at") - timedelta(hours=23))
    results = await asyncio.gather(_run(porg), _run(porg), _run(porg))
    assert sum(results) == 2  # organiser + candidate, exactly once
    assert len(mails(outbox, "interview_reminder")) == 2


async def test_passed_offsets_skipped_and_reschedule_resets(
    porg: POrg, outbox: OutboxMailer, clock: Clock
) -> None:
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (
        await schedule(
            porg.admin, a["application_id"], sid, [internal(porg.admin.user_id)], start=timedelta(minutes=30)
        )
    ).json()
    detail_state = await _state(porg, body["id"])
    assert detail_state == {"1440": "skipped", "60": "skipped"}
    outbox.sent.clear()
    clock.set(_at(body, "starts_at") - timedelta(minutes=10))
    assert await _run(porg) == 0

    clock.reset()
    resp = await porg.admin.client.patch(
        f"{API}/interviews/{body['id']}/reschedule",
        json={
            "start_local": local("UTC", timedelta(days=3)),
            "timezone": "UTC",
            "expected_version": body["version"],
        },
        headers=h(porg.admin),
    )
    assert resp.status_code == 200, resp.text
    assert await _state(porg, body["id"]) == {}
    clock.set(_at(resp.json(), "starts_at") - timedelta(hours=23))
    assert await _run(porg) == 2


async def _state(porg: POrg, interview_id: str) -> dict[str, str]:
    import uuid

    from app.models.interviews import Interview

    async with tenant_session(porg.tenant_id) as db:
        row = await db.get(Interview, uuid.UUID(interview_id))
        assert row is not None
        return dict(row.reminder_state)
