# ruff: noqa: E501
"""Epic 43 story 43.5: recruitment events, public registration with a personal check-in pass, kiosk check-in,
the server-side queue (SKIP LOCKED, ETA, ETag, notifications) and the dashboard."""

import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.core.database import tenant_session
from app.services.campus import queue
from app.services.campus.queue import estimate, rolling_median
from tests.epic12 import sent
from tests.modules.campus.helpers import CAMPUS, PUBLIC, ctx, rows, scalar, token
from tests.modules.demands.helpers import DOrg, h, open_demand
from tests.modules.identity.conftest import new_client

PASS_RE = re.compile(r"/campus/e/([A-Za-z0-9_\-]+)")


@pytest.fixture
async def anon(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    client = new_client(app)
    yield client
    await client.aclose()


async def _event(org: DOrg, stations: int = 2, **kw: object) -> tuple[dict, list[dict]]:  # type: ignore[type-arg]
    d = await open_demand(org.admin, org.client_id, positions=20)
    body = {
        "name": "Walk-in Drive Chennai", "kind": "walk_in", "demand_ids": [d["id"]],
        "starts_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(), "ends_at": (datetime.now(UTC) + timedelta(hours=8)).isoformat(),
        "venue": "Hall A", "notify_channel": "sms", **kw,
    }  # fmt: skip
    r = await org.admin.client.post(f"{CAMPUS}/events", json=body, headers=h(org.admin))
    assert r.status_code == 201, r.text
    ev = r.json()
    st = []
    for i in range(stations):
        s = await org.admin.client.post(f"{CAMPUS}/events/{ev['id']}/stations", json={"label": f"Desk {i + 1}", "interviewer_user_ids": [str(org.admin.user_id)]}, headers=h(org.admin))
        assert s.status_code == 201, s.text
        st.append(s.json())
    o = await org.admin.client.post(f"{CAMPUS}/events/{ev['id']}/open", headers=h(org.admin))
    assert o.status_code == 200, o.text
    return o.json(), st


async def _walk_in(org: DOrg, event_id: str, n: int) -> list[dict]:  # type: ignore[type-arg]
    out = []
    for i in range(n):
        r = await org.admin.client.post(f"{CAMPUS}/events/{event_id}/check-in", json={"walk_in": {"full_name": f"Walker {i}", "email": f"walker{i}-{uuid.uuid4().hex[:4]}@example.com"}}, headers=h(org.admin))
        assert r.status_code == 200, r.text
        out.append(r.json())
    return out


def test_eta_math() -> None:
    assert estimate(0, 6, 20) == 0
    assert estimate(5, 6, 20) == 20
    assert estimate(13, 6, 20) == 60
    assert estimate(3, 0, 20) is None
    assert rolling_median([], 20) == 20
    assert rolling_median([10, 30, 12], 20) == 12


async def test_public_registration_pass_and_qr_checkin(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    ev, _ = await _event(dorg, notify_channel="whatsapp")
    tok = token(ev["registration_url"])
    view = (await anon.get(f"{PUBLIC}/e/{tok}")).json()
    assert view["mode"] == "register" and view["whatsapp_opt_in"] is True and view["demands"]
    body = {"full_name": "Kavya Iyer", "email": "kavya@example.com", "phone": "+919840022222", "notice_id": view["notice"]["notice_id"], "agreed": True, "whatsapp_opt_in": True}
    r = await anon.post(f"{PUBLIC}/e/{tok}", json=body)
    assert r.status_code == 200 and r.json()["status"] == "registered", r.text
    assert r.json()["qr_svg"].startswith("<svg")
    consents = await rows(dorg, "SELECT purpose, source FROM consents ORDER BY purpose")
    assert ("campus", "event_checkin") in consents and ("whatsapp", "event_checkin") in consents
    dup = await anon.post(f"{PUBLIC}/e/{tok}", json=body)
    assert dup.json() == {"status": "already_registered", "checkin_url": None, "qr_svg": None, "challenge": None} or dup.json()["status"] == "already_registered"
    assert dup.json()["checkin_url"] is None
    mail = sent(kind="event_checkin_pass", to="kavya@example.com")
    m = PASS_RE.search(mail[-1].text)
    assert m is not None
    pass_view = (await anon.get(f"{PUBLIC}/e/{m.group(1)}")).json()
    assert pass_view["mode"] == "pass" and pass_view["candidate_first_name"] == "Kavya"
    ci = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/check-in", json={"token": f"https://app.example.com/campus/e/{m.group(1)}"}, headers=h(dorg.admin))
    assert ci.status_code == 200, ci.text
    assert ci.json()["status"] == "checked_in" and ci.json()["queue_position"] == 1 and ci.json()["method"] == "qr"
    reuse = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/check-in", json={"token": m.group(1)}, headers=h(dorg.admin))
    assert reuse.status_code == 410
    app_src = await rows(dorg, "SELECT source, source_ref FROM applications")
    assert app_src == [("event", ci.json()["id"])]


async def test_two_stations_never_get_the_same_entry(dorg: DOrg) -> None:
    ev, stations = await _event(dorg)
    await _walk_in(dorg, ev["id"], 3)
    eid = uuid.UUID(ev["id"])
    async with tenant_session(dorg.tenant_id) as db_a:
        first = await queue.call_next(ctx(dorg), db_a, eid, uuid.UUID(stations[0]["id"]))
        # Session A still holds its row lock: B must skip it (no wait) and take the next entry.
        async with tenant_session(dorg.tenant_id) as db_b:
            second = await queue.call_next(ctx(dorg), db_b, eid, uuid.UUID(stations[1]["id"]))
    assert first is not None and second is not None
    assert first.id != second.id and {first.position, second.position} == {1, 2}


async def test_queue_eta_etag_and_notifications(dorg: DOrg) -> None:
    ev, stations = await _event(dorg)
    await _walk_in(dorg, ev["id"], 4)
    url = f"{CAMPUS}/events/{ev['id']}/queue"
    q = await dorg.admin.client.get(url)
    assert q.status_code == 200 and q.headers["cache-control"] == "no-store"
    waiting = [e for e in q.json()["entries"] if e["status"] == "waiting"]
    assert [e["eta_minutes"] for e in waiting] == [0, 20, 20, 40]
    etag = q.headers["etag"]
    same = await dorg.admin.client.get(url, headers={"If-None-Match": etag})
    assert same.status_code == 304
    call = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/stations/{stations[0]['id']}/call-next", headers=h(dorg.admin))
    assert call.status_code == 200, call.text
    entry = call.json()
    # No registered SMS template / no phone: the notice fails softly and the queue moves on.
    assert entry["status"] == "called" and entry["notified_at"] is not None and entry["notify_result"] in ("no_template", "no_phone", "missing_phone")
    busy = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/stations/{stations[0]['id']}/call-next", headers=h(dorg.admin))
    assert busy.status_code == 409
    changed = await dorg.admin.client.get(url, headers={"If-None-Match": etag})
    assert changed.status_code == 200
    st = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/queue/{entry['id']}/start", headers=h(dorg.admin))
    assert st.status_code == 200 and st.json()["interview_id"], st.text
    assert await scalar(dorg, "SELECT count(*) FROM interviews WHERE id = :i", i=st.json()["interview_id"]) == 1
    done = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/queue/{entry['id']}/complete", headers=h(dorg.admin))
    assert done.status_code == 200 and done.json()["status"] == "done"
    nxt = (await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/stations/{stations[1]['id']}/call-next", headers=h(dorg.admin))).json()
    skip = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/queue/{nxt['id']}/skip", headers=h(dorg.admin))
    assert skip.json()["status"] == "skipped"
    back = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/queue/{nxt['id']}/requeue", headers=h(dorg.admin))
    assert back.json()["status"] == "waiting" and back.json()["position"] == 5
    dash = (await dorg.admin.client.get(f"{CAMPUS}/events/{ev['id']}/dashboard")).json()
    assert dash["registered"] == 4 and dash["checked_in"] == 4 and dash["interviewed"] == 1 and dash["waiting"] == 3
    assert dash["outcomes"].get("interviewing", 0) >= 1


async def test_event_transitions_and_isolation(dorg: DOrg, other_dorg: DOrg) -> None:
    ev, _ = await _event(dorg, stations=0)
    bad = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/open", headers=h(dorg.admin))
    assert bad.status_code == 409
    assert (await other_dorg.admin.client.get(f"{CAMPUS}/events/{ev['id']}")).status_code == 404
    live = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/go-live", headers=h(dorg.admin))
    assert live.json()["status"] == "live"
    closed = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/close", headers=h(dorg.admin))
    assert closed.json()["status"] == "closed" and closed.json()["registration_url"] is None
    late = await dorg.admin.client.post(f"{CAMPUS}/events/{ev['id']}/check-in", json={"walk_in": {"full_name": "Late", "email": "late@example.com"}}, headers=h(dorg.admin))
    assert late.status_code == 409
