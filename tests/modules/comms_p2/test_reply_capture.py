# ruff: noqa: E501
"""Epic 33 FR-171: reply capture for Talent OS-originated threads (fake provider; no real Google / Microsoft)."""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.mailer import EmailMessage
from app.core.settings import get_settings
from app.job.schedules import BEAT_SCHEDULE
from app.models.comms import EmailThread, InboundEmail
from app.models.connections import UserConnection
from app.services.comms import inbound
from app.services.connections.fake import FakeProvider
from app.services.connections.google import GoogleProvider
from app.services.connections.microsoft import MicrosoftProvider
from app.services.connections.provider import SentRef, TokenSet
from app.services.messaging_erasure import CommsChannelsHandler
from app.services.privacy.registry import ErasureContext
from tests.epic10 import API, DOrg, h, member
from tests.modules.comms_p2.helpers import candidate
from tests.modules.identity.conftest import Member


@pytest.fixture
def fake_on(monkeypatch: pytest.MonkeyPatch, _connections_env: FakeProvider) -> FakeProvider:
    monkeypatch.setattr(get_settings(), "calendar_provider_fake", True)
    return _connections_env


async def _connect(m: Member, *, reply_capture: bool) -> None:
    path = "enable-reply-capture" if reply_capture else "start"
    start = await m.client.post(f"{API}/connections/google/{path}", headers=h(m))
    assert start.status_code == 200, start.text
    state = parse_qs(urlparse(start.json()["authorize_url"]).query)["state"][0]
    resp = await m.client.get(
        f"{API}/connections/google/callback", params={"code": "me", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 302 and "connected=google" in resp.headers["location"], resp.headers.get(
        "location"
    )


async def _email(m: Member, cand_id: uuid.UUID) -> str:
    resp = await m.client.post(
        f"{API}/emails",
        json={
            "to": [{"entity_type": "candidate", "entity_id": str(cand_id)}],
            "subject": "Hello",
            "body_html": "<p>Hi</p>",
        },
        headers=h(m),
    )
    assert resp.status_code == 202, resp.text
    return str(resp.json()["message_ids"][0])


async def _sync(tenant_id: uuid.UUID, now: datetime | None = None) -> dict[str, int]:
    async with tenant_session(tenant_id) as db:
        return await inbound.sync_threads(RequestContext(tenant_id=tenant_id), db, now)


async def _thread(tenant_id: uuid.UUID) -> EmailThread:
    async with tenant_session(tenant_id) as db:
        return (await db.execute(select(EmailThread))).scalar_one()


async def test_reply_captured_on_record_once_and_only_that_thread(
    app: FastAPI, dorg: DOrg, fake_on: FakeProvider
) -> None:
    m = dorg.admin
    fake_on.account_email = m.email.lower()
    await _connect(m, reply_capture=True)
    status = (await m.client.get(f"{API}/connections/me/reply-capture")).json()
    assert status["enabled"] is True and status["scopes_granted"] is True
    cand = await candidate(m)
    email_id = await _email(m, cand)
    thread = await _thread(dorg.tenant_id)
    assert {"entity_type": "candidate", "entity_id": str(cand)} in thread.related
    # an own message (the connected account) and the candidate's reply (HTML only -> text)
    fake_on.add_reply(thread.thread_ref, m.email, "my follow-up")
    fake_on.add_reply(
        thread.thread_ref, "priya@example.com", "", body_html="<p>Yes <b>please</b></p><script>x()</script>"
    )
    counts = await _sync(dorg.tenant_id)
    assert counts["replies"] == 1
    assert await _sync(dorg.tenant_id, datetime.now(UTC) + timedelta(minutes=3)) == {
        "threads": 1,
        "replies": 0,
        "errors": 0,
        "stopped": 0,
    }
    async with tenant_session(dorg.tenant_id) as db:
        (reply,) = (await db.execute(select(InboundEmail))).scalars().all()
        assert reply.body_text == "Yes please" and "<" not in reply.body_text
        acts = (
            (await db.execute(text("SELECT entity_id, data FROM activities WHERE kind = 'email_reply'")))
            .mappings()
            .all()
        )
        notes = (
            await db.execute(text("SELECT count(*) FROM notifications WHERE type = 'comms.reply_received'"))
        ).scalar_one()
    assert [a["entity_id"] for a in acts] == [cand] and notes == 1
    # the adapter only ever read this one thread
    reads = [c for c in fake_on.calls if c.startswith("list")]
    assert reads and set(reads) == {f"list_thread_messages:{thread.thread_ref}"}
    thread_view = await m.client.get(f"{API}/emails/{email_id}/thread")
    assert thread_view.status_code == 200 and [r["body_text"] for r in thread_view.json()["replies"]] == [
        "Yes please"
    ]
    # a user who cannot read the record does not see the thread
    outsider = await member(app, dorg, "interviewer", name="Ivan Interviewer")
    assert (await outsider.client.get(f"{API}/emails/{email_id}/thread")).status_code == 404
    await outsider.client.aclose()
    # erasure removes the replies and stops the thread
    async with tenant_session(dorg.tenant_id) as db:
        counts = await CommsChannelsHandler().erase(
            db, ErasureContext(dorg.tenant_id, cand, uuid.uuid4(), "dsr")
        )
        assert counts["inbound_emails"] == 1
        assert (
            await CommsChannelsHandler().verify(db, ErasureContext(dorg.tenant_id, cand, uuid.uuid4(), "dsr"))
            == []
        )
    assert (await _thread(dorg.tenant_id)).status == "stopped"


async def test_no_scope_no_threads_and_disable(dorg: DOrg, fake_on: FakeProvider) -> None:
    m = dorg.admin
    fake_on.account_email = m.email.lower()
    await _connect(m, reply_capture=False)
    assert (await m.client.get(f"{API}/connections/me/reply-capture")).json()["enabled"] is False
    await _email(m, await candidate(m))
    async with tenant_session(dorg.tenant_id) as db:
        assert (await db.execute(text("SELECT count(*) FROM email_threads"))).scalar_one() == 0
    await _connect(m, reply_capture=True)
    await _email(m, await candidate(m, name="Second Person"))
    status = (await m.client.post(f"{API}/connections/me/reply-capture/disable", headers=h(m))).json()
    assert status["enabled"] is False and status["threads_tracked"] == 0


async def test_expiry_and_auth_error(dorg: DOrg, fake_on: FakeProvider) -> None:
    m = dorg.admin
    fake_on.account_email = m.email.lower()
    await _connect(m, reply_capture=True)
    await _email(m, await candidate(m))
    thread = await _thread(dorg.tenant_id)
    assert (await _sync(dorg.tenant_id, thread.sync_until + timedelta(seconds=1)))["stopped"] == 1
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(text("UPDATE email_threads SET status = 'active', next_sync_at = NULL"))
    fake_on.fail_threads = True
    await _sync(dorg.tenant_id)
    assert (await _thread(dorg.tenant_id)).status == "error"
    async with tenant_session(dorg.tenant_id) as db:
        conn = (await db.execute(select(UserConnection))).scalar_one()
        assert conn.status == "needs_reconnect"


def test_latency_target_scheduler_math() -> None:
    cadence = float(BEAT_SCHEDULE["comms-sync-replies"]["schedule"])  # type: ignore[arg-type]
    assert cadence == inbound.SYNC_INTERVAL_SECONDS
    # worst case: reply lands just after a sync; next sync <= cadence later, plus processing well under a minute
    assert cadence + 60 <= inbound.LATENCY_TARGET_SECONDS


# ================================================================ provider adapters (recorded responses)
def _tokens() -> TokenSet:
    return TokenSet("at", "rt", datetime.now(UTC) + timedelta(hours=1), (), "me@example.com")


async def test_microsoft_send_draft_then_send_returns_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/me/messages") and request.method == "POST":
            return httpx.Response(
                201, json={"id": "AAMk1", "conversationId": "CONV-1", "internetMessageId": "<m1@x>"}
            )
        return httpx.Response(202)

    monkeypatch.setattr(
        MicrosoftProvider, "_client", lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    ref = await MicrosoftProvider().send_mail(_tokens(), EmailMessage(to="a@b.com", subject="s", text="t"))
    assert ref == SentRef("AAMk1", "CONV-1", "<m1@x>")
    assert calls == ["POST /v1.0/me/messages", "POST /v1.0/me/messages/AAMk1/send"]


async def test_adapters_read_only_the_given_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []
    raw = base64.urlsafe_b64encode(b"Thanks!").decode().rstrip("=")

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(f"{request.url.host}{request.url.path}?{request.url.query.decode()}")
        if "gmail" in request.url.host:
            msg = {"id": "g2", "internalDate": "1790000000000", "payload": {"mimeType": "text/plain", "body": {"data": raw},
                   "headers": [{"name": "From", "value": "Priya <p@example.com>"}, {"name": "Subject", "value": "Re: hi"}]}}  # fmt: skip
            return httpx.Response(200, json={"id": "T1", "messages": [msg]})
        return httpx.Response(200, json={"value": [{"id": "m2", "from": {"emailAddress": {"address": "p@example.com"}},
                                                    "subject": "Re", "receivedDateTime": "2026-10-05T10:00:00Z",
                                                    "body": {"contentType": "text", "content": "ok"}}]})  # fmt: skip

    def client(self: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(GoogleProvider, "_client", client)
    monkeypatch.setattr(MicrosoftProvider, "_client", client)
    (g,) = await GoogleProvider().list_thread_messages(_tokens(), "T1", None)
    (ms,) = await MicrosoftProvider().list_thread_messages(_tokens(), "CONV-1", None)
    assert g.from_address == "p@example.com" and g.body_text == "Thanks!" and ms.body_text == "ok"
    assert len(urls) == 2
    assert urls[0].startswith("gmail.googleapis.com/gmail/v1/users/me/threads/T1?")
    assert "conversationId+eq+%27CONV-1%27" in urls[1] or "conversationId%20eq%20%27CONV-1%27" in urls[1]
    assert not any("search" in u or "q=" in u for u in urls)
    assert json
