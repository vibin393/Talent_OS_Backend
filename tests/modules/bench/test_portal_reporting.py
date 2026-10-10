"""Worker portal opt-in / opt-out (no cost fields, org RLS), bench reporting and erasure."""

import uuid
from datetime import date, timedelta

from fastapi import FastAPI

from app.core.database import tenant_session
from app.core.security import CSRF_HEADER
from app.schemas.bench_portal import WorkerBenchOut, WorkerBenchState
from app.services.bench_erasure import BenchHandler
from app.services.privacy.registry import ErasureContext
from tests.modules.bench.helpers import BENCH, activate, end_placement, entries, placement
from tests.modules.demands.helpers import DOrg, h
from tests.p2 import sign_in_candidate

PORTAL = "/api/v1/portal/work/bench"
CSRF = {CSRF_HEADER: "csrf-token"}


def test_worker_bench_schema_has_no_cost_fields() -> None:
    for model in (WorkerBenchOut, WorkerBenchState):
        assert not any("cost" in f or "rate" in f for f in model.model_fields)


async def test_contractor_opt_in_and_out(app: FastAPI, dorg: DOrg) -> None:
    active = await placement(dorg, worker_type="contractor")
    cid = uuid.UUID(active["candidate_id"])
    worker = await sign_in_candidate(app, cid, dorg.tenant_id)
    try:
        body = {
            "available_from": date.today().isoformat(),
            "work_authorization": "Indian citizen",
            "preferred_locations": ["Pune"],
        }
        state = (await worker.get(PORTAL)).json()
        assert state == {"entry": None, "can_opt_in": False, "can_opt_out": False}
        refused = await worker.post(f"{PORTAL}/opt-in", json=body, headers=CSRF)
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "opt_in_not_eligible"
        await end_placement(
            dorg, active, date.today() - timedelta(days=1), eligible=False, worker_type="contractor"
        )
        ok = await worker.post(f"{PORTAL}/opt-in", json=body, headers=CSRF)
        assert ok.status_code == 200, ok.text
        assert ok.json()["entry"]["status"] == "on_bench" and ok.json()["can_opt_out"] is True
        assert "cost" not in ok.text
        out = await worker.post(f"{PORTAL}/opt-out", headers=CSRF)
        assert out.status_code == 200 and out.json()["entry"] is None
        (e,) = await entries(dorg, cid)
        assert e["status"] == "released" and e["released_reason"] == "contractor_unavailable"
    finally:
        await worker.aclose()


async def test_worker_cannot_see_another_workers_entry(app: FastAPI, dorg: DOrg) -> None:
    a = await placement(dorg)
    b = await placement(dorg)
    await end_placement(dorg, b, date.today() - timedelta(days=1))
    worker = await sign_in_candidate(app, uuid.UUID(a["candidate_id"]), dorg.tenant_id)
    try:
        assert (await worker.get(PORTAL)).json()["entry"] is None
        assert (await worker.post(f"{PORTAL}/opt-out", headers=CSRF)).status_code == 404
        (eb,) = await entries(dorg, b["candidate_id"])
        assert (await worker.get(f"{BENCH}/entries/{eb['id']}")).status_code in (401, 403)
    finally:
        await worker.aclose()


async def test_summary_and_roll_offs(dorg: DOrg) -> None:
    m = dorg.admin
    p1 = await placement(dorg, rate_unit="day", pay_rate="1000.00")
    await end_placement(dorg, p1, date.today() - timedelta(days=15))
    p2 = await placement(dorg, rate_unit="day", pay_rate="2000.00")
    await end_placement(dorg, p2, date.today() - timedelta(days=15))
    q = await placement(
        dorg, candidate_id=uuid.UUID(p2["candidate_id"]), status="pre_joining", start=date.today()
    )
    await activate(dorg, q)  # p2 redeployed
    await placement(dorg, planned_end=date.today() + timedelta(days=20))
    await placement(dorg, planned_end=date.today() + timedelta(days=45))
    resp = await m.client.get(f"{BENCH}/summary", params={"months": 3})
    assert resp.status_code == 200, resp.text
    s = resp.json()
    assert s["count_on_bench"] == 1 and s["masked"] is False
    assert s["redeployed"] == 1 and s["released"] == 0 and s["redeployment_rate"] == "1.0000"
    assert s["upcoming_roll_offs_30"] == 1 and s["upcoming_roll_offs_60"] == 2
    assert s["total_cost_to_date"][0]["currency"] == "INR"
    days = s["bench_days_by_worker"][0]["bench_days"]
    assert s["total_cost_to_date"][0]["amount"] == f"{days * 1000}.00"
    assert all(mc["currency"] == "INR" for mc in s["cost_by_month"])
    roll = (await m.client.get(f"{BENCH}/roll-offs", params={"within": 60})).json()
    assert len(roll) == 2 and roll[0]["days_left"] == 20


async def test_erasure_deletes_bench_rows(dorg: DOrg) -> None:
    p = await placement(dorg)
    await end_placement(dorg, p, date.today() - timedelta(days=1))
    cid = uuid.UUID(p["candidate_id"])
    handler = BenchHandler()
    ctx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=cid, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(dorg.tenant_id) as db:
        assert (await handler.export(db, ctx)) is not None
        counts = await handler.erase(db, ctx)
        assert counts["entries"] == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert await handler.verify(db, ctx) == []
    assert await entries(dorg, cid) == []


async def test_settings_require_write(dorg: DOrg) -> None:
    resp = await dorg.admin.client.patch(
        f"{BENCH}/settings", json={"upcoming_roll_off_days": 0}, headers=h(dorg.admin)
    )
    assert resp.status_code == 422
