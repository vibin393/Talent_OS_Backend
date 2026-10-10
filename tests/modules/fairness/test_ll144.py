# ruff: noqa: E501
"""Epic 49 A8 / A11: NYC LL144 notice, notice-period score hiding (matching list + pipeline card), alternative-process
requests (Task, hiding, own application only), and the pure business-day arithmetic."""

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import text

from app.core.business_hours import Calendar
from app.core.database import tenant_session
from app.services.fairness.ll144 import add_business_days
from tests.modules.demands.helpers import h
from tests.modules.fairness.helpers import CSRF, F, PORTAL, enable, make_candidate, portal
from tests.modules.fairness.test_reports import seed_matches
from tests.portal.conftest import Site, publish

API = "/api/v1"


def test_business_days_skip_weekends_and_holidays() -> None:
    cal = Calendar.from_settings({}, [date(2026, 10, 9)])
    # Wed 2026-10-07 + 10 business days (Fri 9th is a holiday) -> Thu 2026-10-22
    assert add_business_days(date(2026, 10, 7), 10, cal) == date(2026, 10, 22)
    week = Calendar.from_settings({d: {"start": "09:00", "end": "18:00"} for d in ("mon", "tue", "wed", "thu", "fri", "sat")}, [])
    assert add_business_days(date(2026, 10, 7), 10, week) == date(2026, 10, 19)


async def _app(site: Site, demand_id: str, cid: uuid.UUID) -> str:
    r = await site.admin.client.post(f"{API}/demands/{demand_id}/applications", json={"candidate_ids": [str(cid)], "source": "manual"}, headers=h(site.admin))
    assert r.status_code == 200, r.text
    return str(r.json()["items"][0]["application_id"])


async def test_notice_period_and_alternative_process(app: FastAPI, site: Site, public: object) -> None:
    org = site.org
    ids = await publish(site)
    did, posting = ids["demand_id"], ids["posting_id"]
    notice_url = f"{API}/public/fairness/{site.slug}/notice?posting_id={posting}"
    assert (await public.get(notice_url)).json() == {"applies": False, "notice_text": None, "qualifications_text": None, "data_retention_text": None, "alternative_process_text": None, "aedts": []}  # type: ignore[attr-defined]

    await enable(org, ll144_enabled=True, ll144_notice_text="We use automated tools.", alternative_process_text="Ask for a human review.")
    flag = await org.admin.client.put(f"{F}/demands/{did}/ll144", json={"flagged": True}, headers=h(org.admin))
    assert flag.status_code == 200, flag.text
    assert flag.json()["flagged"] and flag.json()["in_notice_period"] and flag.json()["notice_period_ends_at"]
    inv = (await org.admin.client.get(f"{F}/aedt")).json()
    match_aedt = next(e for e in inv if e["feature"] == "ai_match")
    await org.admin.client.patch(f"{F}/aedt/{match_aedt['id']}", json={"status": "active"}, headers=h(org.admin))
    n = (await public.get(notice_url)).json()  # type: ignore[attr-defined]
    assert n["applies"] is True and n["notice_text"] == "We use automated tools." and n["aedts"][0]["name"] == "AI Match"

    a, b = await make_candidate(org, "Ann A"), await make_candidate(org, "Ben B")
    app_a, app_b = await _app(site, did, a), await _app(site, did, b)
    await seed_matches(org.tenant_id, uuid.UUID(did), {a: 88, b: 64})

    # day 0 of the notice period: AEDT outputs hidden on the match list and the pipeline card
    ml = await org.admin.client.get(f"{API}/demands/{did}/matches")
    assert ml.status_code == 200, ml.text
    for item in ml.json()["items"]:
        assert item["overall"] is None and item["sub_scores"] == {} and item["explanation"] is None
        assert item["score_hidden_reason"] == "notice_period"
    card = await org.admin.client.get(f"{API}/applications/{app_a}")
    assert card.status_code == 200 and card.json()["match_score"] is None and card.json()["score_hidden_reason"] == "notice_period"

    # after the notice period the scores show again
    async with tenant_session(org.tenant_id) as db:
        await db.execute(text("UPDATE aedt_jurisdiction_flags SET notice_published_at = :t"), {"t": datetime.now(UTC) - timedelta(days=40)})
    ml = (await org.admin.client.get(f"{API}/demands/{did}/matches")).json()
    assert {i["overall"] for i in ml["items"]} == {88.0, 64.0}
    assert all(i["score_hidden_reason"] is None for i in ml["items"])

    # alternative process from the Candidate Portal: own application only
    ca = await portal(app, org, a)
    try:
        other = await ca.post(f"{PORTAL}/alternative-requests", json={"application_id": app_b}, headers=CSRF)
        assert other.status_code == 404
        req = await ca.post(f"{PORTAL}/alternative-requests", json={"application_id": app_a, "note": "Screen reader user"}, headers=CSRF)
        assert req.status_code == 201, req.text
        assert req.json()["channel"] == "portal" and req.json()["task_id"]
        mine = await ca.get(f"{PORTAL}/alternative-requests")
        assert [r["application_id"] for r in mine.json()] == [app_a]
    finally:
        await ca.aclose()
    async with tenant_session(org.tenant_id) as db:
        title = (await db.execute(text("SELECT title FROM tasks WHERE entity_id = :a"), {"a": uuid.UUID(app_a)})).scalar_one()
    assert title == "Alternative selection process requested"
    ml = (await org.admin.client.get(f"{API}/demands/{did}/matches")).json()
    hidden = {i["candidate"]["id"]: i for i in ml["items"]}
    assert hidden[str(a)]["overall"] is None and hidden[str(a)]["score_hidden_reason"] == "alternative_process"
    assert hidden[str(b)]["overall"] == 64.0
    card = (await org.admin.client.get(f"{API}/applications/{app_a}")).json()
    assert card["score_hidden_reason"] == "alternative_process"
    # the recruiter sees the request on the Application (never the self-ID)
    seen = await org.admin.client.get(f"{F}/applications/{app_a}/alternative-request")
    assert seen.status_code == 200 and seen.json()["status"] == "open"
    assert (await org.admin.client.get(f"{F}/applications/{app_b}/alternative-request")).json() is None

    # unflag -> the notice is withdrawn
    off = await org.admin.client.put(f"{F}/demands/{did}/ll144", json={"flagged": False}, headers=h(org.admin))
    assert off.status_code == 200 and off.json()["flagged"] is False
    assert (await public.get(notice_url)).json()["applies"] is False  # type: ignore[attr-defined]


async def test_alternative_not_offered_without_flag(app: FastAPI, site: Site) -> None:
    ids = await publish(site)
    cid = await make_candidate(site.org)
    app_id = await _app(site, ids["demand_id"], cid)
    c = await portal(app, site.org, cid)
    try:
        r = await c.post(f"{PORTAL}/alternative-requests", json={"application_id": app_id}, headers=CSRF)
        assert r.status_code == 404 and r.json()["error"]["code"] == "alternative_not_offered"
    finally:
        await c.aclose()
