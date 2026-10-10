# ruff: noqa: E501
"""Epic 72 provider contract tests (Google Calendar v3 / Microsoft Graph) on ``httpx.MockTransport`` fixtures shaped
like the providers' documentation. No network: every request is answered by the handler and recorded."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.services.connections.google import GoogleProvider
from app.services.connections.microsoft import TOS_TAG_PROP, MicrosoftProvider
from app.services.connections.provider import ChannelRef, EventSpec, SyncTokenInvalid, TokenSet

TOKENS = TokenSet("access-x", "refresh-x", datetime.now(UTC) + timedelta(hours=1), (), "org@example.com")
WINDOW = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC))
SECRET_TITLE = "Secret board meeting about layoffs"


def _mock(monkeypatch: pytest.MonkeyPatch, provider: Any, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(provider, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    return seen


def _no_content_fields(seen: list[httpx.Request]) -> None:
    for r in seen:
        q = str(r.url.params).lower()
        assert "summary" not in q and "subject" not in q and "description" not in q and "body" not in q


# ------------------------------------------------------------------------------------------------ Google
async def test_google_watch_stop_and_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    g = GoogleProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/watch"):
            body = json.loads(request.content)
            assert body["type"] == "web_hook" and body["token"] == "chan-secret" and body["id"] == "c1"
            return httpx.Response(200, json={"kind": "api#channel", "id": "c1", "resourceId": "res-1", "expiration": "1767225600000"})
        if request.url.path.endswith("/channels/stop"):
            return httpx.Response(404)
        body = json.loads(request.content)
        assert body["extendedProperties"]["private"]["tos_tag"] == "tos1:abc:2:deadbeef"
        return httpx.Response(200, json={"id": "evt1", "etag": '"e1"'})

    seen = _mock(monkeypatch, g, handler)
    ref = await g.watch(TOKENS, "https://x/api/v1/hooks/calendar/c1", "c1", "chan-secret", 7 * 86400)
    assert ref.resource_id == "res-1" and ref.expires_at is not None
    await g.stop_watch(TOKENS, ref)  # 404 ignored
    spec = EventSpec("uid@tos", "s", "d", WINDOW[0], WINDOW[0] + timedelta(hours=1), "UTC", (), tos_tag="tos1:abc:2:deadbeef")
    result = await g.upsert_event(TOKENS, spec)
    assert result.etag == '"e1"'
    assert "chan-secret" not in repr(ref)
    assert len(seen) == 3


async def test_google_list_changes_filters_and_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    g = GoogleProvider()
    items = [
        {"id": "evt1", "iCalUID": "uid1@tos", "status": "confirmed", "etag": '"e2"',
         "start": {"dateTime": "2026-10-20T10:00:00Z"}, "end": {"dateTime": "2026-10-20T11:00:00Z"},
         "organizer": {"self": True}, "extendedProperties": {"private": {"tos_tag": "tos1:x:0:aa"}},
         "attendees": [{"email": "Cand@Example.com", "responseStatus": "accepted"}, {"email": "b@x.com", "responseStatus": "tentative"}]},
        {"id": "foreign", "iCalUID": "other@x", "status": "confirmed", "summary": SECRET_TITLE},
        {"id": "evt3", "iCalUID": "uid3@tos", "status": "cancelled"},
    ]  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        assert "fields" in request.url.params and "summary" not in request.url.params["fields"]
        if request.url.params.get("syncToken") == "stale":
            return httpx.Response(410, json={"error": {"code": 410}})
        if "pageToken" not in request.url.params:
            return httpx.Response(200, json={"items": items[:2], "nextPageToken": "p2"})
        return httpx.Response(200, json={"items": items[2:], "nextSyncToken": "sync-2"})

    seen = _mock(monkeypatch, g, handler)
    changes, token = await g.list_changes(TOKENS, "sync-1", known_ids=frozenset({"evt1", "evt3"}), known_ical_uids=frozenset(), window=WINDOW)
    assert token == "sync-2"
    assert [c.provider_event_id for c in changes] == ["evt1", "evt3"]
    assert SECRET_TITLE not in repr(changes)
    first = changes[0]
    assert first.tos_tag == "tos1:x:0:aa" and first.etag == '"e2"'
    assert {(a.email, a.response) for a in first.attendees} == {("cand@example.com", "accepted"), ("b@x.com", "tentative")}
    assert changes[1].status == "deleted"
    assert "syncToken" in seen[0].url.params and "timeMin" not in seen[0].url.params
    _no_content_fields(seen)
    with pytest.raises(SyncTokenInvalid):
        await g.list_changes(TOKENS, "stale", known_ids=frozenset(), known_ical_uids=frozenset(), window=WINDOW)


async def test_google_initial_listing_uses_window(monkeypatch: pytest.MonkeyPatch) -> None:
    g = GoogleProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "timeMin" in request.url.params and "syncToken" not in request.url.params
        return httpx.Response(200, json={"items": [], "nextSyncToken": "first"})

    _mock(monkeypatch, g, handler)
    assert await g.list_changes(TOKENS, None, known_ids=frozenset(), known_ical_uids=frozenset(), window=WINDOW) == ([], "first")


# ------------------------------------------------------------------------------------------------ Microsoft
async def test_microsoft_subscription_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    m = MicrosoftProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/subscriptions"):
            body = json.loads(request.content)
            assert body["clientState"] == "cs" and body["resource"] == "me/events"
            assert body["lifecycleNotificationUrl"].endswith("?lifecycle=1")
            return httpx.Response(201, json={"id": "sub-1", "expirationDateTime": "2026-10-17T10:00:00Z"})
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": "sub-1"})
        if request.method == "DELETE":
            return httpx.Response(404)
        body = json.loads(request.content)
        assert body["singleValueExtendedProperties"][0]["id"] == TOS_TAG_PROP
        return httpx.Response(201, json={"id": "ms-evt", "changeKey": "ck1"})

    _mock(monkeypatch, m, handler)
    ref = await m.watch(TOKENS, "https://x/h", "c2", "cs", 10080 * 60)
    assert ref.resource_id == "sub-1"
    renewed = await m.renew_watch(TOKENS, ref, 3600)
    assert renewed.resource_id == "sub-1"
    await m.stop_watch(TOKENS, ref)
    res = await m.upsert_event(TOKENS, EventSpec("u", "s", "d", WINDOW[0], WINDOW[0] + timedelta(hours=1), "UTC", (), tos_tag="tos1:a:0:bb"))
    assert res.etag == "ck1"


async def test_microsoft_delta_removed_detail_and_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    m = MicrosoftProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "deltatoken=bad" in str(request.url):
            return httpx.Response(410, json={"error": {"code": "syncStateNotFound"}})
        if path.endswith("/calendarView/delta"):
            assert "startDateTime" in request.url.params
            return httpx.Response(200, json={
                "value": [
                    {"id": "ms-evt", "iCalUId": "ical-1"},
                    {"id": "foreign", "iCalUId": "zzz", "subject": SECRET_TITLE},
                    {"id": "gone", "@removed": {"reason": "deleted"}},
                ],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=t2",
            })  # fmt: skip
        if path.endswith("/me/events/ms-evt"):
            assert "subject" not in request.url.params["$select"] and "body" not in request.url.params["$select"]
            return httpx.Response(200, json={
                "id": "ms-evt", "iCalUId": "ical-1", "changeKey": "ck2", "isCancelled": False,
                "start": {"dateTime": "2026-10-20T10:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-10-20T11:00:00.0000000", "timeZone": "UTC"},
                "attendees": [
                    {"emailAddress": {"address": "a@x.com"}, "status": {"response": "tentativelyAccepted"}},
                    {"emailAddress": {"address": "b@x.com"}, "status": {"response": "notResponded"}},
                    {"emailAddress": {"address": "c@x.com"}, "status": {"response": "declined"}},
                ],
                "singleValueExtendedProperties": [{"id": TOS_TAG_PROP, "value": "tos1:i:1:cc"}],
            })  # fmt: skip
        raise AssertionError(f"unexpected {request.url}")

    seen = _mock(monkeypatch, m, handler)
    changes, link = await m.list_changes(TOKENS, None, known_ids=frozenset({"ms-evt", "gone"}), known_ical_uids=frozenset(), window=WINDOW)
    assert link.endswith("t2")
    assert [c.provider_event_id for c in changes] == ["ms-evt", "gone"]
    assert changes[1].status == "deleted"
    got = changes[0]
    assert got.tos_tag == "tos1:i:1:cc" and got.etag == "ck2" and got.start_utc == datetime(2026, 10, 20, 10, tzinfo=UTC)
    assert [a.response for a in got.attendees] == ["tentative", "needs_action", "declined"]
    assert SECRET_TITLE not in repr(changes)
    assert not any(r.url.path.endswith("/foreign") for r in seen)
    with pytest.raises(SyncTokenInvalid):
        await m.list_changes(TOKENS, "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=bad", known_ids=frozenset(), known_ical_uids=frozenset(), window=WINDOW)


def test_channel_ref_repr_has_no_secret() -> None:
    assert "token" not in repr(ChannelRef("c", "r", None)).lower()
