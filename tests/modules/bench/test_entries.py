"""Bench entries: handlers (PlacementEnded / Activated, submissions), manual add, release, cost, masking,
isolation."""

import asyncio
import uuid
from datetime import date, timedelta

from fastapi import FastAPI
from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.services.placements import bench
from tests.modules.bench.helpers import BENCH, activate, end_placement, entries, placement, role_member
from tests.modules.demands.helpers import DOrg, h, open_demand
from tests.modules.offers.helpers import add_application, move


async def test_employee_placement_end_creates_one_entry_with_cost(dorg: DOrg) -> None:
    end = date(2026, 10, 31)
    p = await placement(dorg, rate_unit="hour", pay_rate="437.50", start=date(2026, 6, 1))
    await end_placement(dorg, p, end)
    await end_placement(dorg, p, end)  # re-delivery
    rows = await entries(dorg, p["candidate_id"])
    assert len(rows) == 1
    e = rows[0]
    assert e["status"] == "on_bench" and e["origin"] == "placement_ended"
    assert e["available_from"] == date(2026, 11, 1)
    assert str(e["cost_per_day"]) == "3500.00" and e["cost_currency"] == "INR"
    assert e["cost_source"] == "rate_segment" and e["cost_rate_unit_basis"] == "hourx8.00"
    resp = await dorg.admin.client.get(f"{BENCH}/entries/{e['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (
        body["cost_per_day"] == "3500.00" and body["cost_masked"] is False and body["cost_unknown"] is False
    )


async def test_contractor_or_ineligible_end_creates_nothing(dorg: DOrg) -> None:
    p = await placement(dorg, worker_type="contractor")
    await end_placement(dorg, p, date.today(), eligible=False, worker_type="contractor")
    assert await entries(dorg, p["candidate_id"]) == []


async def test_monthly_rate_and_unsupported_unit(dorg: DOrg) -> None:
    p = await placement(dorg, rate_unit="month", pay_rate="100000.00", bill_rate="150000.00")
    await end_placement(dorg, p, date.today() - timedelta(days=1))
    (e,) = await entries(dorg, p["candidate_id"])
    assert str(e["cost_per_day"]) == "4545.45"
    q = await placement(dorg, rate_unit="fixed", pay_rate="5000.00", bill_rate="9000.00")
    await end_placement(dorg, q, date.today() - timedelta(days=1))
    (f,) = await entries(dorg, q["candidate_id"])
    assert f["cost_per_day"] is None
    body = (await dorg.admin.client.get(f"{BENCH}/entries/{f['id']}")).json()
    assert body["cost_unknown"] is True and body["cost_per_day"] is None


async def test_concurrent_placement_ended_deliveries_one_entry(dorg: DOrg) -> None:
    p = await placement(dorg)
    payload = {
        "placement_id": p["id"],
        "candidate_id": p["candidate_id"],
        "end_date": date.today().isoformat(),
        "bench_eligible": True,
    }
    ctx = RequestContext(tenant_id=dorg.tenant_id, actor_kind="system")

    async def deliver() -> None:
        from sqlalchemy.exc import IntegrityError

        try:
            async with tenant_session(dorg.tenant_id) as db:
                await bench.on_placement_ended(ctx, db, payload)
        except IntegrityError:  # mapped to a no-op by the task wrapper
            pass

    await asyncio.gather(*(deliver() for _ in range(20)))
    rows = await entries(dorg, p["candidate_id"])
    assert len([r for r in rows if r["status"] in ("on_bench", "proposed")]) == 1


async def reject(org: DOrg, application_id: uuid.UUID) -> None:
    from app.services import pipeline_service
    from tests.modules.demands.helpers import admin_ctx

    async with tenant_session(org.tenant_id) as db:
        await pipeline_service.transition_application(
            admin_ctx(org), db, application_id, "rejected", "user", "client_rejected"
        )


async def test_submission_proposes_and_rejection_returns_to_bench(dorg: DOrg) -> None:
    p = await placement(dorg)
    cid = uuid.UUID(p["candidate_id"])
    await end_placement(dorg, p, date.today() - timedelta(days=3))
    d1 = await open_demand(dorg.admin, dorg.client_id, positions=1)
    d2 = await open_demand(dorg.admin, dorg.client_id, positions=1, title="Platform Engineer")
    a1 = await add_application(dorg.admin, uuid.UUID(d1["id"]), cid)
    a2 = await add_application(dorg.admin, uuid.UUID(d2["id"]), cid)
    await move(dorg, a1, ("shortlisted", "user"), ("submitted", "system"))
    await move(dorg, a2, ("shortlisted", "user"), ("submitted", "system"))
    (e,) = await entries(dorg, cid)
    assert e["status"] == "proposed" and set(e["proposed_application_ids"]) == {a1, a2}
    await reject(dorg, a1)
    (e,) = await entries(dorg, cid)
    assert e["status"] == "proposed" and e["proposed_application_ids"] == [a2]
    await reject(dorg, a2)
    (e,) = await entries(dorg, cid)
    assert e["status"] == "on_bench" and e["proposed_application_ids"] == []


async def test_new_placement_activated_redeploys(dorg: DOrg) -> None:
    p = await placement(dorg)
    cid = uuid.UUID(p["candidate_id"])
    await end_placement(dorg, p, date.today() - timedelta(days=3))
    await activate(dorg, p)  # same (source) placement: no change
    assert (await entries(dorg, cid))[0]["status"] == "on_bench"
    q = await placement(dorg, candidate_id=cid, status="pre_joining", start=date.today())
    await activate(dorg, q)
    (e,) = await entries(dorg, cid)
    assert e["status"] == "redeployed" and e["redeployed_placement_id"] == uuid.UUID(q["id"])
    assert e["closed_at"] is not None


async def test_manual_add_conflict_release_and_cost_override(dorg: DOrg) -> None:
    from tests.modules.offers.helpers import create_candidate

    m = dorg.admin
    cid = await create_candidate(m, "Bina Bench")
    body = {"candidate_id": str(cid), "available_from": date.today().isoformat(), "notes": "Between projects"}
    resp = await m.client.post(f"{BENCH}/entries", json=body, headers=h(m))
    assert resp.status_code == 201, resp.text
    entry = resp.json()
    assert entry["origin"] == "manual" and entry["cost_unknown"] is True
    dup = await m.client.post(f"{BENCH}/entries", json=body, headers=h(m))
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "bench_entry_open"
    assert dup.json()["error"]["details"]["bench_entry_id"] == entry["id"]

    cost = await m.client.post(
        f"{BENCH}/entries/{entry['id']}/cost",
        json={"cost_per_day": "4545.45", "currency": "INR"},
        headers=h(m),
    )
    assert cost.status_code == 200, cost.text
    assert cost.json()["cost_per_day"] == "4545.45" and cost.json()["cost_source"] == "manual"

    patched = await m.client.patch(
        f"{BENCH}/entries/{entry['id']}", json={"notes": "Updated", "refresh_skills": True}, headers=h(m)
    )
    assert patched.status_code == 200 and patched.json()["notes"] == "Updated"

    no_reason = await m.client.post(f"{BENCH}/entries/{entry['id']}/release", json={}, headers=h(m))
    assert no_reason.status_code == 422
    other = await m.client.post(
        f"{BENCH}/entries/{entry['id']}/release", json={"reason": "other"}, headers=h(m)
    )
    assert other.status_code == 422
    ok = await m.client.post(
        f"{BENCH}/entries/{entry['id']}/release", json={"reason": "resigned"}, headers=h(m)
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "released" and ok.json()["released_reason"] == "resigned"
    again = await m.client.post(
        f"{BENCH}/entries/{entry['id']}/release", json={"reason": "resigned"}, headers=h(m)
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "transition_not_allowed"
    # a new open entry may be created after a terminal one
    resp = await m.client.post(f"{BENCH}/entries", json=body, headers=h(m))
    assert resp.status_code == 201

    listed = (await m.client.get(f"{BENCH}/entries", params={"status": ["on_bench"], "q": "Bina"})).json()
    assert [i["candidate_id"] for i in listed["items"]] == [str(cid)]


async def test_settings_roundtrip(dorg: DOrg) -> None:
    m = dorg.admin
    got = (await m.client.get(f"{BENCH}/settings")).json()
    assert got["hours_per_day"] == "8.00" and got["bench_day_basis"] == "weekdays"
    resp = await m.client.patch(
        f"{BENCH}/settings", json={"bench_day_basis": "calendar", "hours_per_day": "9.00"}, headers=h(m)
    )
    assert resp.status_code == 200 and resp.json()["hours_per_day"] == "9.00"


async def test_cost_masked_without_compensation_view(app: FastAPI, dorg: DOrg) -> None:
    p = await placement(dorg, rate_unit="day", pay_rate="2500.00")
    await end_placement(dorg, p, date.today() - timedelta(days=5))
    (e,) = await entries(dorg, p["candidate_id"])
    viewer = await role_member(app, dorg, "bench_viewer", {"bench:read": "tenant"}, "Vic Viewer")
    try:
        one = (await viewer.client.get(f"{BENCH}/entries/{e['id']}")).json()
        assert one["cost_per_day"] is None and one["cost_to_date"] is None and one["cost_masked"] is True
        listed = (await viewer.client.get(f"{BENCH}/entries")).json()
        assert all(i["cost_per_day"] is None for i in listed["items"])
        summ = (await viewer.client.get(f"{BENCH}/summary")).json()
        assert summ["masked"] is True and all(t["amount"] is None for t in summ["total_cost_to_date"])
        # no bench:write -> 403
        resp = await viewer.client.post(
            f"{BENCH}/entries/{e['id']}/release", json={"reason": "resigned"}, headers=h(viewer)
        )
        assert resp.status_code == 403
    finally:
        await viewer.client.aclose()


async def test_cross_tenant_404(dorg: DOrg, other_dorg: DOrg) -> None:
    p = await placement(dorg)
    await end_placement(dorg, p, date.today() - timedelta(days=2))
    (e,) = await entries(dorg, p["candidate_id"])
    o = other_dorg.admin
    eid = e["id"]
    assert (await o.client.get(f"{BENCH}/entries/{eid}")).status_code == 404
    assert (
        await o.client.patch(f"{BENCH}/entries/{eid}", json={"notes": "x"}, headers=h(o))
    ).status_code == 404
    assert (
        await o.client.post(
            f"{BENCH}/entries/{eid}/cost", json={"cost_per_day": "1", "currency": "INR"}, headers=h(o)
        )
    ).status_code == 404
    assert (
        await o.client.post(f"{BENCH}/entries/{eid}/release", json={"reason": "resigned"}, headers=h(o))
    ).status_code == 404
    resp = await o.client.post(
        f"{BENCH}/entries",
        json={"candidate_id": p["candidate_id"], "available_from": date.today().isoformat()},
        headers=h(o),
    )
    assert resp.status_code == 404
    listed = (await o.client.get(f"{BENCH}/entries")).json()
    assert listed["total"] == 0
    d = await open_demand(dorg.admin, dorg.client_id)
    assert (await o.client.get(f"{BENCH}/demands/{d['id']}/matches")).status_code == 404
    assert (
        await o.client.post(
            f"{BENCH}/demands/{d['id']}/reviews",
            json={"candidate_id": p["candidate_id"], "decision": "reviewed"},
            headers=h(o),
        )
    ).status_code == 404
    assert (
        await o.client.post(f"{BENCH}/demands/{d['id']}/reviews/complete", headers=h(o))
    ).status_code == 404


async def test_bench_registers_no_posting_source_and_writes_no_postings(dorg: DOrg) -> None:
    from app.services import postings

    kinds = set(getattr(postings, "posting_sources", lambda: {})())
    assert not any(str(k).startswith("bench") for k in kinds)
    p = await placement(dorg)
    await end_placement(dorg, p, date.today() - timedelta(days=2))
    async with tenant_session(dorg.tenant_id) as db:
        for table in ("billing_postings", "payable_postings"):
            n = (await db.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            assert n == 0
