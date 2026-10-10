# ruff: noqa: E501, F811
"""Epic 72 stories 72.1-72.4 end to end on the Fake provider: links + version tag, responses, decline / move / delete
rules, echoes and the loop guard, ingress verification and coalescing, channel renewal / fallback / revocation, the
organisation free/busy fallback and the ★ guarantees (inbound data never cancels or moves an Interview)."""

import json
import pathlib
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.database import tenant_session
from app.models.calendar_sync import CalendarChannel, CalendarEventLink
from app.models.scheduling_agent import SchedulingRequest
from app.services.calendar_sync import ingress
from app.services.connections import fake as fake_provider
from app.tasks import calendar_sync as cal_tasks
from tests.calendar_sync.conftest import (
    API,
    COrg,
    _agent_env,
    _agents_registered,
    _connections_env,
    _env,
    channel,
    corg,
    interview,
    link,
    make_corg,
    participants,
    put_settings,
    sync,
    tasks,
)
from tests.modules.identity.conftest import csrf


def _event(c: COrg) -> fake_provider.FakeEvent:
    evs = [e for e in c.fake.events.values() if e.tos_tag]
    assert len(evs) == 1
    return evs[0]


# ------------------------------------------------------------------------------------------------ 72.1 links + responses
async def test_channel_link_and_tag_written(corg: COrg) -> None:
    ch = await channel(corg)
    assert ch.status == "active" and ch.provider_resource_id and ch.sync_token is not None
    assert ch.expires_at is not None and ch.renew_after is not None and ch.renew_after < ch.expires_at
    lk = await link(corg)
    ev = _event(corg)
    assert lk.provider_event_id == ev.id and lk.state == "in_sync" and lk.tos_tag == ev.tos_tag
    assert lk.tos_tag.startswith(f"tos1:{corg.interview_id}:")
    assert lk.tos_etag == ev.etag
    assert str(ch.channel_id) in corg.fake.channels


async def test_responses_update_participants(corg: COrg) -> None:
    ev = _event(corg)
    parts = await participants(corg)
    cand = parts["candidate"]
    corg.fake.respond(ev.id, cand.email, "accepted")
    await sync(corg)
    assert (await participants(corg))["candidate"].response_status == "accepted"
    corg.fake.respond(ev.id, cand.email, "tentative")
    await sync(corg)
    assert (await participants(corg))["candidate"].response_status == "tentative"
    assert not await tasks(corg)


async def test_candidate_decline_creates_reschedule_task_when_agent_off(corg: COrg) -> None:
    ev = _event(corg)
    cand = (await participants(corg))["candidate"]
    corg.fake.respond(ev.id, cand.email, "declined")
    await sync(corg)
    assert (await participants(corg))["candidate"].response_status == "declined"
    assert any(t.startswith("Reschedule requested from") for t in await tasks(corg))
    assert (await interview(corg)).status == "scheduled"


async def test_decline_with_agent_on_opens_reschedule_request(
    app: FastAPI, _connections_env: fake_provider.FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.scheduling_agent import requests as sched_requests

    async def no_run(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(sched_requests, "start_run", no_run)
    c = await make_corg(app, _connections_env, monkeypatch, agent=True)
    try:
        ev = _event(c)
        cand = (await participants(c))["candidate"]
        c.fake.respond(ev.id, cand.email, "declined")
        await sync(c)
        async with tenant_session(c.tenant_id) as db:
            reqs = list((await db.execute(select(SchedulingRequest).where(SchedulingRequest.interview_id == c.interview_id))).scalars())
        assert len(reqs) == 1 and reqs[0].kind == "reschedule" and reqs[0].requested_by_kind == "candidate"
        assert "calendar_declined" in (reqs[0].reschedule_reason or "")
        assert not await tasks(c)
    finally:
        await c.k.admin.client.aclose()


# ------------------------------------------------------------------------------------------------ 72.2 move / delete rules
async def test_move_restore_and_echo(corg: COrg) -> None:
    row = await interview(corg)
    ev = _event(corg)
    writes = corg.fake.writes
    corg.fake.move_event(ev.id, row.starts_at + timedelta(days=1), row.ends_at + timedelta(days=1))
    await sync(corg)
    assert (await link(corg)).state == "restoring"
    assert ev.start == row.starts_at and corg.fake.writes == writes + 1  # put back to the Talent OS time
    assert any(t.startswith("Reschedule requested from") for t in await tasks(corg))
    await sync(corg)  # our own restore comes back: an echo
    assert (await link(corg)).state == "in_sync"
    assert corg.fake.writes == writes + 1
    after = await interview(corg)
    assert after.starts_at == row.starts_at and after.status == "scheduled"


async def test_move_keep_as_proposal(corg: COrg) -> None:
    await put_settings(corg.k, move_policy="keep_as_proposal")
    row = await interview(corg)
    ev = _event(corg)
    writes = corg.fake.writes
    moved = row.starts_at + timedelta(hours=5)
    corg.fake.move_event(ev.id, moved, moved + timedelta(hours=1))
    await sync(corg)
    lk = await link(corg)
    assert lk.state == "moved" and lk.seen_start == moved and corg.fake.writes == writes
    assert ev.start == moved
    resp = await corg.client.get(f"{API}/calendar-sync/interviews/{corg.interview_id}/sync")
    assert resp.status_code == 200 and resp.json()["move_policy"] == "keep_as_proposal"
    assert resp.json()["links"][0]["state"] == "moved"
    keep = await corg.client.post(f"{API}/calendar-sync/interviews/{corg.interview_id}/keep-talent-os-time", headers=csrf(corg.client))
    assert keep.status_code == 200 and keep.json()["status"] == "restored"
    assert ev.start == row.starts_at and (await link(corg)).state == "in_sync"


async def test_alternating_moves_terminate_in_conflict(corg: COrg) -> None:
    row = await interview(corg)
    ev = _event(corg)
    writes = corg.fake.writes
    for i in range(1, 6):
        corg.fake.move_event(ev.id, row.starts_at + timedelta(hours=i), row.ends_at + timedelta(hours=i))
        await sync(corg)
        await sync(corg)  # the echo of a restore (if any)
    lk = await link(corg)
    assert lk.state == "conflict"
    assert corg.fake.writes - writes == 3  # restore_limit_per_hour, then no more provider writes
    assert "Calendar keeps moving this interview" in await tasks(corg)
    assert (await interview(corg)).starts_at == row.starts_at


async def test_delete_creates_task_never_cancels_then_recreate_and_dismiss(corg: COrg) -> None:
    ev = _event(corg)
    corg.fake.delete_event(ev.id)
    await sync(corg)
    assert (await interview(corg)).status == "scheduled"
    titles = await tasks(corg)
    assert any(t.startswith("Interview event removed from") for t in titles)
    async with tenant_session(corg.tenant_id) as db:
        state = (await db.execute(select(CalendarEventLink.state).where(CalendarEventLink.provider_event_id == ev.id))).scalar_one()
    assert state == "deleted"
    resp = await corg.client.post(f"{API}/calendar-sync/interviews/{corg.interview_id}/recreate-event", headers=csrf(corg.client))
    assert resp.status_code == 200 and resp.json()["status"] == "recreated"
    new = await link(corg)
    assert new.provider_event_id != ev.id and new.state == "in_sync"
    # a second deletion, dismissed this time
    corg.fake.delete_event(new.provider_event_id)
    await sync(corg)
    resp = await corg.client.post(f"{API}/calendar-sync/interviews/{corg.interview_id}/dismiss-change", headers=csrf(corg.client))
    assert resp.status_code == 200
    async with tenant_session(corg.tenant_id) as db:
        states = set((await db.execute(select(CalendarEventLink.state).where(CalendarEventLink.interview_id == corg.interview_id))).scalars())
    assert states == {"detached"}
    assert (await interview(corg)).status == "scheduled"


async def test_full_resync_after_token_invalidation(corg: COrg) -> None:
    ev = _event(corg)
    cand = (await participants(corg))["candidate"]
    corg.fake.invalidate_sync_token()
    corg.fake.respond(ev.id, cand.email, "accepted")
    result = await sync(corg)
    assert result["status"] == "ok"
    assert (await participants(corg))["candidate"].response_status == "accepted"
    assert (await channel(corg)).sync_token.startswith(f"{corg.fake.token_epoch}:")


async def test_foreign_events_never_leave_the_provider(corg: COrg, caplog: pytest.LogCaptureFixture) -> None:
    row = await interview(corg)
    secret = "Secret board meeting about layoffs"
    corg.fake.add_foreign_event(corg.k.admin.email, secret, row.starts_at, row.ends_at)
    result = await sync(corg, full=True)
    assert result["changes"] == 0 or all(o in ("echo", "responses") for o in result["outcomes"])
    async with tenant_session(corg.tenant_id) as db:
        assert len(list((await db.execute(select(CalendarEventLink))).scalars())) == 1
    assert secret not in caplog.text and secret not in json.dumps(result, default=str)


# ------------------------------------------------------------------------------------------------ 72.3 ingress + channels
async def test_ingress_google_verification_and_coalescing(corg: COrg) -> None:
    from app.services.connections.store import get_store

    ch = await channel(corg)
    cid = str(ch.channel_id)
    url = f"{API}/hooks/calendar/{cid}"
    bad = await corg.client.post(url, headers=corg.fake.notify_google(cid, token="wrong"))
    assert bad.status_code == 401
    sync_state = await corg.client.post(url, headers=corg.fake.notify_google(cid, state="sync"))
    assert sync_state.status_code == 200
    unknown = await corg.client.post(f"{API}/hooks/calendar/{uuid.uuid4()}", headers=corg.fake.notify_google(cid))
    assert unknown.status_code == 404
    before = corg.fake.delta_calls
    cal_tasks.FOLLOWUPS.clear()
    for _ in range(10):
        ok = await corg.client.post(url, headers=corg.fake.notify_google(cid))
        assert ok.status_code == 200
    assert corg.fake.delta_calls - before == 1  # coalesced
    await get_store().pop(f"calendar:delta:{cid}")
    again = await corg.client.post(url, headers=corg.fake.notify_google(cid))
    assert again.status_code == 200 and corg.fake.delta_calls - before == 2
    assert len(cal_tasks.FOLLOWUPS) <= 1
    assert (await channel(corg)).last_notification_at is not None


async def test_ingress_microsoft_validation_and_forged_client_state(corg: COrg) -> None:
    ch = await channel(corg)
    cid = str(ch.channel_id)
    url = f"{API}/hooks/calendar/{cid}"
    hand = await corg.client.post(url, params={"validationToken": "abc 123"})
    assert hand.status_code == 200 and hand.text == "abc 123" and hand.headers["content-type"].startswith("text/plain")
    before_bad = ingress.METRICS["calendar_bad_client_state"]
    before = corg.fake.delta_calls
    forged = await corg.client.post(url, content=json.dumps(corg.fake.notify_microsoft(cid, client_state="forged")))
    assert forged.status_code == 202 and corg.fake.delta_calls == before
    assert ingress.METRICS["calendar_bad_client_state"] == before_bad + 1
    good = await corg.client.post(url, content=json.dumps(corg.fake.notify_microsoft(cid)))
    assert good.status_code == 202 and corg.fake.delta_calls == before + 1


async def test_kill_switch_acknowledges_without_sync(corg: COrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.schemas.ecosystem import SuspendState
    from app.services import ecosystem_service

    async def suspended(tenant_id: uuid.UUID, **_: object) -> SuspendState:
        return SuspendState(suspended=True, scope="tenant", reason="test")

    monkeypatch.setattr(ecosystem_service, "is_suspended", suspended)
    ch = await channel(corg)
    before = corg.fake.delta_calls
    resp = await corg.client.post(f"{API}/hooks/calendar/{ch.channel_id}", headers=corg.fake.notify_google(str(ch.channel_id)))
    assert resp.status_code == 200 and corg.fake.delta_calls == before


async def test_renewal_rewatch_and_failure_fallback(corg: COrg) -> None:
    from app.services import calendar_sync_service as svc

    old = await channel(corg)
    async with tenant_session(corg.tenant_id) as db:
        await db.execute(update(CalendarChannel).values(renew_after=datetime.now(old.created_at.tzinfo) - timedelta(minutes=1)))
    async with tenant_session(corg.tenant_id) as db:
        assert await svc.renew_due(db) == 1
    new = await channel(corg)
    assert new.channel_id != old.channel_id and new.status == "active"
    assert str(old.channel_id) in corg.fake.stopped_channels
    # renewal failure -> polling (15 min) with a daily re-watch
    corg.fake.fail_watch = True
    async with tenant_session(corg.tenant_id) as db:
        await db.execute(update(CalendarChannel).values(renew_after=datetime.now(old.created_at.tzinfo) - timedelta(minutes=1)))
    async with tenant_session(corg.tenant_id) as db:
        await svc.renew_due(db)
    polled = await channel(corg)
    assert polled.status == "polling" and polled.next_poll_at is not None and polled.consecutive_failures == 1
    assert polled.renew_after is not None and polled.renew_after > datetime.now(polled.renew_after.tzinfo) + timedelta(hours=23)
    async with tenant_session(corg.tenant_id) as db:
        await db.execute(update(CalendarChannel).values(next_poll_at=datetime.now(old.created_at.tzinfo) - timedelta(minutes=1)))
    before = corg.fake.delta_calls
    async with tenant_session(corg.tenant_id) as db:
        assert await svc.poll_due(db) == 1
    assert corg.fake.delta_calls == before + 1


async def test_revoked_connection_stops_channel_and_asks_to_reconnect(corg: COrg) -> None:
    from app.services import connections_service

    ch = await channel(corg)
    async with tenant_session(corg.tenant_id) as db:
        await connections_service.mark_needs_reconnect(db, ch.connection_id, "test")
    assert (await channel(corg)).status == "stopped"
    assert "Reconnect your calendar so interview changes reach Talent OS" in await tasks(corg)
    resp = await corg.client.get(f"{API}/calendar-sync/interviews/{corg.interview_id}/sync")
    assert resp.status_code == 200
    assert {p["state"] for p in resp.json()["participants"]} == {"not_syncing"}


async def test_settings_api_and_me(corg: COrg) -> None:
    got = await corg.client.get(f"{API}/calendar-sync/settings")
    assert got.status_code == 200 and got.json()["enabled"] is True and got.json()["poll_minutes"] == 15
    bad = await corg.client.put(f"{API}/calendar-sync/settings", json={"poll_minutes": 2}, headers=csrf(corg.client))
    assert bad.status_code == 422
    me = await corg.client.get(f"{API}/calendar-sync/me")
    assert me.status_code == 200 and me.json()["channels"][0]["status"] == "active"
    admin = await corg.client.get(f"{API}/calendar-sync/channels")
    assert admin.status_code == 200 and "@" not in json.dumps(admin.json())


# ------------------------------------------------------------------------------------------------ 72.4 organisation free/busy
async def test_org_free_busy_fallback_and_optout(corg: COrg, app: FastAPI) -> None:
    from app.services.calendar_sync import org_free_busy
    from tests.modules.identity.conftest import join

    member = await join(app, corg.k.porg.tenant, name="Noor Unconnected")
    row = await interview(corg)
    seen: list[list[str]] = []

    async def source(db: object, emails: list[str], start: datetime, end: datetime) -> dict[str, list[tuple[datetime, datetime]]]:
        seen.append(emails)
        return {e: [(row.starts_at, row.ends_at)] for e in emails}

    org_free_busy.set_source(source)
    try:
        await put_settings(corg.k, org_source_enabled=True)
        body = {"user_ids": [str(member.user_id), str(corg.k.admin.user_id)], "start_utc": (row.starts_at - timedelta(hours=1)).isoformat(), "end_utc": (row.ends_at + timedelta(hours=1)).isoformat()}
        resp = await corg.client.post(f"{API}/connections/free-busy", json=body, headers=csrf(corg.client))
        assert resp.status_code == 200, resp.text
        items = {i["user_id"]: i for i in resp.json()["items"]}
        assert items[str(member.user_id)]["status"] == "available" and items[str(member.user_id)]["source"] == "organisation"
        assert items[str(corg.k.admin.user_id)]["source"] is None  # personal connection, never the org source
        assert seen == [[member.email.lower()]]
        opt = await member.client.put(f"{API}/calendar-sync/me/free-busy-optout", json={"opted_out": True}, headers=csrf(member.client))
        assert opt.status_code == 200 and opt.json()["free_busy_opted_out"] is True
        from app.services.connections.store import get_store

        get_store().clear()  # type: ignore[attr-defined]
        again = await corg.client.post(f"{API}/connections/free-busy", json=body, headers=csrf(corg.client))
        assert {i["user_id"]: i for i in again.json()["items"]}[str(member.user_id)]["status"] == "unknown"
    finally:
        org_free_busy.set_source(None)
        await member.client.aclose()


# ------------------------------------------------------------------------------------------------ guarantees
def test_p1_spec_has_no_tag_without_provider() -> None:
    from app.services import connections_service
    from app.services.calendar_sync import links

    connections_service.set_event_tag_provider(None)
    try:
        assert connections_service.event_tag(uuid.uuid4(), 0) is None
    finally:
        connections_service.set_event_tag_provider(links.make_tag)
    assert connections_service.event_tag(uuid.uuid4(), 1).startswith("tos1:")  # type: ignore[union-attr]


def test_calendar_sync_never_reaches_p1_cancel_or_reschedule() -> None:
    root = pathlib.Path(__file__).resolve().parents[2] / "app" / "services" / "calendar_sync"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("cancel_interview", "reschedule_interview", "interviews.scheduling", "interviews.outcomes"):
            assert forbidden not in text, f"{path.name} references {forbidden}"


async def test_cross_tenant_channel_is_isolated(corg: COrg, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    ch = await channel(corg)
    other = await make_corg(app, corg.fake, monkeypatch)
    try:
        resp = await other.client.get(f"{API}/calendar-sync/interviews/{corg.interview_id}/sync")
        assert resp.status_code == 404
        async with tenant_session(other.tenant_id) as db:
            assert (await db.execute(select(CalendarChannel).where(CalendarChannel.channel_id == ch.channel_id))).scalar_one_or_none() is None
    finally:
        await other.k.admin.client.aclose()
