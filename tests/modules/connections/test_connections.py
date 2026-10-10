"""7.4 calendar / mailbox connections with the fake provider (never a real Google / Microsoft call)."""

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from loguru import logger
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.mailer import OutboxMailer
from app.core.settings import get_settings
from app.models.connections import UserConnection
from app.models.interviews import Interview
from app.services.connections.fake import FakeProvider
from app.services.connections.provider import BusyBlock
from tests.modules.identity.conftest import Member, join
from tests.modules.interviews.helpers import API, external, h, internal, mails, plan, schedule, submitted_app
from tests.modules.pipeline.helpers import POrg


@pytest.fixture
def fake_on(monkeypatch: pytest.MonkeyPatch, _connections_env: FakeProvider) -> FakeProvider:
    monkeypatch.setattr(get_settings(), "calendar_provider_fake", True)
    return _connections_env


async def connect(m: Member, provider: str = "google", code: str = "organiser") -> dict[str, str]:
    start = await m.client.post(f"{API}/connections/{provider}/start", headers=h(m))
    assert start.status_code == 200, start.text
    state = parse_qs(urlparse(start.json()["authorize_url"]).query)["state"][0]
    resp = await m.client.get(
        f"{API}/connections/{provider}/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    return {"state": state, "location": resp.headers["location"]}


async def test_not_configured_falls_back_to_ics(porg: POrg, outbox: OutboxMailer) -> None:
    providers = (await porg.admin.client.get(f"{API}/connections/providers")).json()
    assert {p["provider"]: p["configured"] for p in providers} == {"google": False, "microsoft": False}
    resp = await porg.admin.client.post(f"{API}/connections/google/start", headers=h(porg.admin))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "provider_not_configured"
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    await schedule(porg.admin, a["application_id"], sid, [internal(porg.admin.user_id)])
    assert all(m.attachments for m in mails(outbox, "interview_invite"))


async def test_connect_callback_and_state_reuse(porg: POrg, fake_on: FakeProvider) -> None:
    fake_on.account_email = porg.admin.email.lower()
    done = await connect(porg.admin)
    assert done["location"].endswith("/settings/connections?connected=google")
    reuse = await porg.admin.client.get(
        f"{API}/connections/google/callback",
        params={"code": "again", "state": done["state"]},
        follow_redirects=False,
    )
    assert reuse.status_code == 302 and reuse.headers["location"].endswith("error=invalid_state")
    forged = await porg.admin.client.get(
        f"{API}/connections/google/callback", params={"code": "x", "state": "abc.def"}, follow_redirects=False
    )
    assert forged.headers["location"].endswith("error=invalid_state")
    listing = await porg.admin.client.get(f"{API}/connections")
    assert listing.status_code == 200
    items = listing.json()
    assert (
        len(items) == 1
        and items[0]["status"] == "active"
        and items[0]["account_email"] == porg.admin.email.lower()
    )
    assert "token" not in listing.text.lower().replace("token_", "")
    for item in items:
        assert not any("token" in k for k in item)


async def test_state_bound_to_user(app: FastAPI, porg: POrg, fake_on: FakeProvider) -> None:
    other = await join(app, porg.tenant, name="Mallory Member")
    try:
        start = await porg.admin.client.post(f"{API}/connections/google/start", headers=h(porg.admin))
        state = parse_qs(urlparse(start.json()["authorize_url"]).query)["state"][0]
        stolen = await other.client.get(
            f"{API}/connections/google/callback", params={"code": "x", "state": state}, follow_redirects=False
        )
        assert stolen.headers["location"].endswith("error=invalid_state")
    finally:
        await other.client.aclose()
    _ = fake_on


async def test_connected_organiser_creates_event_without_ics(
    porg: POrg, fake_on: FakeProvider, outbox: OutboxMailer
) -> None:
    fake_on.account_email = porg.admin.email.lower()
    await connect(porg.admin)
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (
        await schedule(
            porg.admin,
            a["application_id"],
            sid,
            [internal(porg.admin.user_id), external("ext@partner.example.com")],
            meeting_url=None,
            create_online_meeting=True,
        )
    ).json()
    assert len(fake_on.events) == 1
    event = next(iter(fake_on.events.values()))
    got = (await porg.admin.client.get(f"{API}/interviews/{body['id']}")).json()
    assert got["meeting_url"] == f"https://meet.fake/{event.id}" and got["provider"] == "google"
    assert mails(outbox, "interview_invite") == []  # the provider invites the attendees
    # External interviewers still get their scorecard link (sent from the organiser's mailbox).
    assert [m.kind for m in fake_on.sent] == ["interview_scorecard_link"]
    assert "/l/" in fake_on.sent[0].text

    # Reschedule updates the same provider event.
    resp = await porg.admin.client.patch(
        f"{API}/interviews/{body['id']}/reschedule",
        json={"start_local": "2031-01-10T10:00", "timezone": "UTC", "expected_version": got["version"]},
        headers=h(porg.admin),
    )
    assert resp.status_code == 200 and len(fake_on.events) == 1 and event.updates == 1

    # Disconnect: revoke called, tokens wiped; new invitations fall back to .ics.
    conn_id = (await porg.admin.client.get(f"{API}/connections")).json()[0]["id"]
    resp = await porg.admin.client.delete(f"{API}/connections/{conn_id}", headers=h(porg.admin))
    assert resp.status_code == 200 and resp.json()["status"] == "revoked"
    assert fake_on.revoked
    async with tenant_session(porg.tenant_id) as db:
        row = await db.get(UserConnection, uuid.UUID(conn_id))
        assert row is not None and row.status == "revoked"
        left = await db.execute(
            text("SELECT count(*) FROM vault_secrets WHERE owner_type = 'calendar_connection' AND owner_id = :id"),
            {"id": uuid.UUID(conn_id)},
        )
        assert left.scalar_one() == 0
    b = await submitted_app(porg, "Rahul Sharma")
    await schedule(porg.admin, b["application_id"], sid, [internal(porg.admin.user_id)])
    assert len(mails(outbox, "interview_invite")) == 2 and len(fake_on.events) == 1


async def test_refresh_failure_marks_needs_reconnect_and_falls_back(
    porg: POrg, fake_on: FakeProvider, outbox: OutboxMailer
) -> None:
    fake_on.account_email = porg.admin.email.lower()
    await connect(porg.admin)
    async with tenant_session(porg.tenant_id) as db:
        row = (await db.execute(select(UserConnection))).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    fake_on.fail_refresh = True
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    await schedule(porg.admin, a["application_id"], sid, [internal(porg.admin.user_id)])
    assert fake_on.events == {}
    assert len(mails(outbox, "interview_invite")) == 2
    status = (await porg.admin.client.get(f"{API}/connections")).json()[0]["status"]
    assert status == "needs_reconnect"


async def test_provider_auth_error_falls_back(
    porg: POrg, fake_on: FakeProvider, outbox: OutboxMailer
) -> None:
    fake_on.account_email = porg.admin.email.lower()
    await connect(porg.admin)
    fake_on.fail_upsert = True
    a = await submitted_app(porg)
    sid = (await plan(porg.admin, porg.demand_id))[0]["id"]
    body = (await schedule(porg.admin, a["application_id"], sid, [internal(porg.admin.user_id)])).json()
    assert len(mails(outbox, "interview_invite")) == 2
    async with tenant_session(porg.tenant_id) as db:
        iv = await db.get(Interview, uuid.UUID(body["id"]))
        assert iv is not None and iv.provider == "none"
    assert (await porg.admin.client.get(f"{API}/connections")).json()[0]["status"] == "needs_reconnect"


async def test_free_busy(app: FastAPI, porg: POrg, fake_on: FakeProvider) -> None:
    fake_on.account_email = porg.admin.email.lower()
    await connect(porg.admin)
    start = datetime(2031, 1, 6, 8, 0, tzinfo=UTC)
    fake_on.busy[porg.admin.email.lower()] = [
        BusyBlock(start + timedelta(hours=2), start + timedelta(hours=3))
    ]
    other = await join(app, porg.tenant, name="Una Unconnected")
    try:
        body = {
            "user_ids": [str(porg.admin.user_id), str(other.user_id)],
            "start_utc": start.isoformat(),
            "end_utc": (start + timedelta(hours=12)).isoformat(),
        }
        resp = await porg.admin.client.post(f"{API}/connections/free-busy", json=body, headers=h(porg.admin))
        assert resp.status_code == 200, resp.text
        items = {i["user_id"]: i for i in resp.json()["items"]}
        mine = items[str(porg.admin.user_id)]
        assert mine["status"] == "available" and len(mine["busy"]) == 1
        assert items[str(other.user_id)] == {"user_id": str(other.user_id), "status": "unknown", "busy": [], "source": None}
        too_long = await porg.admin.client.post(
            f"{API}/connections/free-busy",
            json={**body, "end_utc": (start + timedelta(days=15)).isoformat()},
            headers=h(porg.admin),
        )
        assert too_long.status_code == 422
    finally:
        await other.client.aclose()


async def test_tokens_absent_from_logs(porg: POrg, fake_on: FakeProvider) -> None:
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    try:
        fake_on.account_email = porg.admin.email.lower()
        await connect(porg.admin)
        await porg.admin.client.get(f"{API}/connections")
    finally:
        logger.remove(sink)
    joined = "\n".join(lines)
    assert "fake-access-" not in joined and "fake-refresh-" not in joined


async def test_connections_cross_tenant(porg: POrg, other_porg: POrg, fake_on: FakeProvider) -> None:
    fake_on.account_email = porg.admin.email.lower()
    await connect(porg.admin)
    conn_id = (await porg.admin.client.get(f"{API}/connections")).json()[0]["id"]
    o = other_porg.admin
    resp = await o.client.delete(f"{API}/connections/{conn_id}", headers=h(o))
    assert resp.status_code == 404
    assert (await o.client.get(f"{API}/connections")).json() == []
    fb = await o.client.post(
        f"{API}/connections/free-busy",
        json={
            "user_ids": [str(porg.admin.user_id)],
            "start_utc": "2031-01-06T08:00:00Z",
            "end_utc": "2031-01-06T20:00:00Z",
        },
        headers=h(o),
    )
    assert fb.status_code == 200 and fb.json()["items"][0]["status"] == "unknown"
