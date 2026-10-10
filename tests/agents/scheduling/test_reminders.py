# ruff: noqa: E501
"""Epic 59 story 59.5: reminder rows, agent-sent reminders, the P1 suppression hook (C-11) with its 10-minute fallback,
disabling the agent restores P1, scorecard nudges stop on submit, reschedule / cancel supersede rows."""

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.interviews import Interview
from app.models.scheduling_agent import SchedulingReminder
from app.services import ai, interviews_service
from app.services.ai.fake_llm import FakeTurn, fake_llm
from app.services.interviews import common as p1_common
from app.services.scheduling_agent import common as sched_common
from app.tasks import scheduling_agent as sched_tasks
from tests.agents.scheduling.conftest import KOrg, calls, done
from tests.modules.interviews.helpers import h, internal, schedule


async def _book(k: KOrg, hours: int = 30) -> uuid.UUID:
    resp = await schedule(
        k.admin,
        str(k.application_id),
        str(k.stage_id),
        [internal(k.admin.user_id)],
        tz="Asia/Kolkata",
        start=timedelta(hours=hours),
    )
    return uuid.UUID(resp.json()["id"])


async def _rows(k: KOrg, interview_id: uuid.UUID) -> list[SchedulingReminder]:
    async with tenant_session(k.tenant_id) as db:
        rows = list(
            (
                await db.execute(
                    select(SchedulingReminder)
                    .where(SchedulingReminder.interview_id == interview_id)
                    .order_by(SchedulingReminder.due_at)
                )
            ).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


def _at(value) -> None:  # noqa: ANN001
    p1_common.set_clock(lambda: value)
    sched_common.set_clock(lambda: value)


def _reset() -> None:
    p1_common.set_clock(None)
    sched_common.set_clock(None)


async def _state(k: KOrg, interview_id: uuid.UUID) -> dict:
    async with tenant_session(k.tenant_id) as db:
        row = await db.get(Interview, interview_id)
        assert row is not None
        return dict(row.reminder_state or {})


async def test_rows_agent_sends_and_p1_skips(korg: KOrg) -> None:
    k = korg
    iid = await _book(k)
    rows = await _rows(k, iid)
    assert {(r.offset_minutes, r.status) for r in rows} == {(1440, "due"), (60, "due")} and len(
        rows
    ) == 4  # interviewer + candidate
    starts_at = (await interviews_service_get(k, iid)).starts_at
    try:
        _at(starts_at - timedelta(minutes=1440) + timedelta(minutes=2))
        # P1: the agent owns reminders and this one is < 10 minutes overdue -> skipped (nothing marked)
        async with tenant_session(k.tenant_id) as db:
            assert await interviews_service.send_due_reminders(db) == 0
        assert (await _state(k, iid)).get("1440") is None
        due_ids = [r.id for r in rows if r.offset_minutes == 1440]
        with fake_llm(
            {
                "agent_scheduling": [
                    calls(*[("reminders.send", {"reminder_id": str(i)}) for i in due_ids]),
                    done(),
                ]
            }
        ):
            async with tenant_session(k.tenant_id) as db:
                out = await sched_tasks.tick(db)
        assert out["reminder_runs"] == 1
        after = {r.id: r for r in await _rows(k, iid)}
        assert all(after[i].status == "sent" for i in due_ids), [(after[i].status, after[i].channel, after[i].candidate_id) for i in due_ids]
        assert {after[i].channel for i in due_ids} == {"email", "in_app"}
        assert (await _state(k, iid)).get("1440") == "agent"  # P1 never duplicates
        _at(starts_at - timedelta(minutes=1440) + timedelta(minutes=30))
        async with tenant_session(k.tenant_id) as db:
            assert await interviews_service.send_due_reminders(db) == 0
    finally:
        _reset()


async def interviews_service_get(k: KOrg, iid: uuid.UUID) -> Interview:
    async with tenant_session(k.tenant_id) as db:
        row = await db.get(Interview, iid)
        assert row is not None
        db.expunge(row)
        return row


async def test_p1_fallback_after_ten_minutes_and_disabling_restores_p1(korg: KOrg) -> None:
    k = korg
    iid = await _book(k)
    starts_at = (await interviews_service_get(k, iid)).starts_at
    try:
        _at(
            starts_at - timedelta(minutes=1440) + timedelta(minutes=11)
        )  # the agent never sent it: P1 sends it now
        async with tenant_session(k.tenant_id) as db:
            assert await interviews_service.send_due_reminders(db) == 2
        assert (await _state(k, iid)).get("1440") not in (None, "agent", "skipped")
        # disabling the agent restores P1 immediately (no grace)
        iid2 = await _book(k, hours=40)
        starts2 = (await interviews_service_get(k, iid2)).starts_at
        async with tenant_session(k.tenant_id) as db:
            await ai.set_features(db, k.tenant_id, {"agent_scheduling": False})
        _at(starts2 - timedelta(minutes=1440) + timedelta(minutes=1))
        async with tenant_session(k.tenant_id) as db:
            assert await interviews_service.send_due_reminders(db) >= 2
        assert (await _state(k, iid2)).get("1440") not in (None, "agent", "skipped")
    finally:
        _reset()


async def test_reschedule_and_cancel_supersede_rows(korg: KOrg) -> None:
    k = korg
    iid = await _book(k)
    row = await interviews_service_get(k, iid)
    from tests.modules.interviews.helpers import local

    resp = await k.admin.client.patch(
        f"/api/v1/interviews/{iid}/reschedule",
        json={
            "start_local": local("Asia/Kolkata", timedelta(hours=50)),
            "timezone": "Asia/Kolkata",
            "expected_version": row.version,
        },
        headers=h(k.admin),
    )
    assert resp.status_code == 200, resp.text
    rows = await _rows(k, iid)
    assert {r.status for r in rows if r.sequence == 0} == {"superseded"} and {
        r.status for r in rows if r.sequence == 1
    } == {"due"}
    resp = await k.admin.client.post(
        f"/api/v1/interviews/{iid}/cancel", json={"reason": "role closed"}, headers=h(k.admin)
    )
    assert resp.status_code == 200, resp.text
    assert {r.status for r in await _rows(k, iid)} == {"superseded"}


async def test_scorecard_nudges_until_submitted(korg: KOrg) -> None:
    k = korg
    iid = await _book(k, hours=3)
    row = await interviews_service_get(k, iid)
    try:
        _at(row.ends_at + timedelta(minutes=5))
        async with tenant_session(k.tenant_id) as db:
            await interviews_service.complete_interview(k.ctx(), db, iid)
        nudges = [r for r in await _rows(k, iid) if r.kind == "scorecard_nudge"]
        assert len(nudges) == 2 and {r.offset_minutes for r in nudges} == {1440, 2880}
        assert {r.status for r in await _rows(k, iid) if r.kind == "before_interview"} <= {
            "superseded",
            "due",
        }
        _at(row.ends_at + timedelta(hours=24, minutes=1))
        first = min(nudges, key=lambda r: r.due_at)
        with fake_llm(
            {"agent_scheduling": [calls(("reminders.send", {"reminder_id": str(first.id)})), done()]}
        ):
            async with tenant_session(k.tenant_id) as db:
                out = await sched_tasks.tick(db)
        assert out["nudge_runs"] == 1
        sent = {r.id: r for r in await _rows(k, iid)}
        assert sent[first.id].status == "sent" and sent[first.id].channel == "in_app"
        # P1 never repeats it (reminded_at set)
        async with tenant_session(k.tenant_id) as db:
            assert await interviews_service.scorecard_reminders(db) == 0
        # submitting the scorecard stops the next nudge
        resp = await k.admin.client.post(
            f"/api/v1/interviews/{iid}/scorecards/mine/submit",
            json={"ratings": [], "answers": [], "recommendation": "yes", "summary": "ok"},
            headers=h(k.admin),
        )
        if resp.status_code == 200:
            remaining = [r for r in await _rows(k, iid) if r.kind == "scorecard_nudge" and r.id != first.id]
            assert {r.status for r in remaining} == {"superseded"}
    finally:
        _reset()


def test_fake_turn_import() -> None:
    assert FakeTurn("emit_result", result={}).kind == "emit_result"
