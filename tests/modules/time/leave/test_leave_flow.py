"""Leave requests end to end (Epic 26): Worker request -> AM approval -> balance + draft Timesheet prefill;
balance / overlap / day-count refusals; withdraw; cancellation guard; Decimal strings; isolation."""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from fastapi import FastAPI

from app.core.actors import ActorRef
from app.core.database import tenant_session
from app.schemas.leave import LeaveRequestIn
from app.services.time import leave_requests
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.time.helpers import TIME, WORK, admin_decide, detail, first_timesheet, worker_submit
from tests.modules.time.leave.helpers import (
    LEAVE,
    balance,
    csrf,
    decide,
    friday_of,
    grant,
    internal,
    leave_type,
    request,
    setup,
)


def _no_floats(payload: Any) -> None:
    if isinstance(payload, float):
        raise AssertionError(f"float in payload: {payload}")
    if isinstance(payload, dict):
        for v in payload.values():
            _no_floats(v)
    if isinstance(payload, list):
        for v in payload:
            _no_floats(v)


async def _timesheet_for(s: Any, org: DOrg, offset_weeks: int) -> dict[str, Any]:
    start = (s.start + timedelta(weeks=offset_weeks)).isoformat()
    resp = await org.admin.client.get(f"{TIME}/timesheets", params={"placement_id": s.pid})
    rows = [r for r in resp.json() if r["kind"] == "regular" and r["period_start"] == start]
    assert rows, resp.json()
    return await detail(org, rows[0]["id"])


def _leave_cells(d: dict[str, Any]) -> list[tuple[str, str, str]]:
    return sorted(
        (e["entry_date"], e["category"], e["hours"])
        for e in d["entries"]
        if e["category"].startswith("leave")
    )


async def test_acceptance_half_days_approval_balance_prefill_and_guard(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    el = await leave_type(dorg)
    await grant(dorg, s, el["id"], "5.00")
    fri = friday_of(s)
    mon = fri + timedelta(days=3)
    # preview: Fri pm + Mon am across the weekend = 1.00 day
    resp = await s.worker.get(
        f"{LEAVE}/preview",
        params={
            "leave_type_id": el["id"],
            "start_date": fri.isoformat(),
            "end_date": mon.isoformat(),
            "start_half": "pm",
            "end_half": "am",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["days"] == "1.00" and resp.json()["available"] == "5.00" and resp.json()["sufficient"]
    resp = await request(s, el["id"], fri, mon, start_half="pm", end_half="am", reason="Family visit")
    assert resp.status_code == 201, resp.text
    lv = resp.json()
    _no_floats(lv)
    assert lv["status"] == "requested" and lv["days"] == "1.00" and lv["human_id"].startswith("LV-")
    b = await balance(dorg, s, el["id"])
    assert (b["balance"], b["pending"], b["available"]) == ("5.00", "1.00", "4.00")
    # the Account Manager approves through the approvals engine
    assert (await decide(dorg, lv["id"])).status_code == 200
    assert (await internal(dorg, lv["id"]))["status"] == "approved"
    b = await balance(dorg, s, el["id"])
    assert (b["balance"], b["pending"], b["available"]) == ("4.00", "0.00", "4.00")
    # the draft Timesheets show Leave 4.00 h on Fri and on Mon (prefilled)
    wk1, wk2 = await _timesheet_for(s, dorg, 0), await _timesheet_for(s, dorg, 1)
    assert _leave_cells(wk1) == [(fri.isoformat(), "leave_paid", "4.00")]
    assert _leave_cells(wk2) == [(mon.isoformat(), "leave_paid", "4.00")]
    assert all(e["prefilled"] for e in wk1["entries"] if e["category"] == "leave_paid")
    # ledger: one request_debit of -1.00
    hist = (
        await dorg.admin.client.get(
            f"{TIME}/leave-balances/history", params={"candidate_id": str(s.candidate_id)}
        )
    ).json()
    assert [(r["entry_kind"], r["days"]) for r in hist if r["entry_kind"] == "request_debit"] == [
        ("request_debit", "-1.00")
    ]
    # submit + approve week 1 -> cancelling is refused with adjustment_required
    resp = await worker_submit(s, wk1["id"])
    assert resp.status_code == 200, resp.text
    resp = await s.worker.post(f"{LEAVE}/{lv['id']}/cancel", headers=csrf())
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "timesheet_recall_required", resp.text
    assert (await admin_decide(dorg, wk1["id"])).status_code == 200
    assert (await detail(dorg, wk1["id"]))["status"] == "approved"
    resp = await s.worker.post(f"{LEAVE}/{lv['id']}/cancel", headers=csrf())
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "adjustment_required", resp.text
    # nightly verification finds no mismatch
    resp = await dorg.admin.client.post(f"{TIME}/leave-balances/verify", headers=h(dorg.admin))
    assert resp.status_code == 200 and resp.json()["mismatches"] == []


async def test_cancel_on_draft_then_approve_credits_and_removes_prefill(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    el = await leave_type(dorg)
    await grant(dorg, s, el["id"], "3.00")
    day = s.start + timedelta(days=1)
    lv = (await request(s, el["id"], day, day)).json()
    assert (await decide(dorg, lv["id"])).status_code == 200
    assert _leave_cells(await _timesheet_for(s, dorg, 0)) == [(day.isoformat(), "leave_paid", "8.00")]
    resp = await s.worker.post(f"{LEAVE}/{lv['id']}/cancel", headers=csrf())
    assert resp.status_code == 200 and resp.json()["status"] == "cancellation_requested", resp.text
    # replaying the cancellation approval callback posts one credit only
    assert (await decide(dorg, lv["id"])).status_code == 200
    d = await internal(dorg, lv["id"])
    assert d["status"] == "cancelled"
    from app.services.approvals import FinalDecision

    async with tenant_session(dorg.tenant_id) as db:
        await leave_requests.on_final_decided(
            admin_ctx(dorg),
            db,
            FinalDecision(
                d["approval_request_id"],
                "leave_request",
                uuid.UUID(lv["id"]),
                "approved",
                ActorRef("user", dorg.admin.user_id),
                None,
            ),
        )
    b = await balance(dorg, s, el["id"])
    assert (b["balance"], b["pending"]) == ("3.00", "0.00")
    assert _leave_cells(await _timesheet_for(s, dorg, 0)) == []


async def test_refusals_balance_overlap_no_working_days_half_day_and_floats(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    el = await leave_type(dorg)
    await grant(dorg, s, el["id"], "2.00")
    mon = s.start
    # 3.00 days with 2.00 available -> balance_insufficient
    resp = await request(s, el["id"], mon, mon + timedelta(days=2))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "balance_insufficient", resp.text
    # weekend only -> no_working_days
    resp = await request(s, el["id"], mon + timedelta(days=5), mon + timedelta(days=6))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "no_working_days", resp.text
    # single day with both halves -> 422
    resp = await request(s, el["id"], mon, mon, start_half="pm", end_half="am")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "half_day_invalid", resp.text
    # overlap
    assert (await request(s, el["id"], mon, mon)).status_code == 201
    resp = await request(s, el["id"], mon, mon + timedelta(days=1))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "leave_overlap"
    # adjustments: float refused, more than 2 decimals refused, reason required
    base = {
        "candidate_id": str(s.candidate_id),
        "leave_type_id": el["id"],
        "legal_entity_id": s.placement["legal_entity_id"],
        "reason": "x",
    }
    for days in (1.5, "1.555"):
        resp = await dorg.admin.client.post(
            f"{TIME}/leave-balances/adjust", json={**base, "days": days}, headers=h(dorg.admin)
        )
        assert resp.status_code == 422, resp.text
    resp = await dorg.admin.client.post(
        f"{TIME}/leave-balances/adjust", json={**base, "days": "-1.5", "reason": ""}, headers=h(dorg.admin)
    )
    assert resp.status_code == 422
    # negative below zero refused when negatives are off
    resp = await dorg.admin.client.post(
        f"{TIME}/leave-balances/adjust",
        json={**base, "days": "-9.00", "leave_year": str(s.start.year)},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "balance_insufficient"


async def test_negative_allowed_and_withdraw(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    neg = await leave_type(dorg, allow_negative=True, negative_limit="2.00")
    await grant(dorg, s, neg["id"], "2.00")
    mon = s.start
    lv = await request(s, neg["id"], mon, mon + timedelta(days=2))
    assert lv.status_code == 201, lv.text
    assert (await decide(dorg, lv.json()["id"])).status_code == 200
    assert (await balance(dorg, s, neg["id"]))["balance"] == "-1.00"
    # withdraw releases pending
    nxt = mon + timedelta(days=3)
    lv2 = (await request(s, neg["id"], nxt, nxt)).json()
    assert (await balance(dorg, s, neg["id"]))["pending"] == "1.00"
    resp = await s.worker.post(f"{LEAVE}/{lv2['id']}/withdraw", headers=csrf())
    assert resp.status_code == 200 and resp.json()["status"] == "withdrawn"
    assert (await balance(dorg, s, neg["id"]))["pending"] == "0.00"
    # beyond the negative limit -> refused
    resp = await request(s, neg["id"], mon + timedelta(days=7), mon + timedelta(days=8))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "balance_insufficient"


async def test_reject_requires_comment_and_releases_pending(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    el = await leave_type(dorg)
    await grant(dorg, s, el["id"], "2.00")
    lv = (await request(s, el["id"], s.start, s.start)).json()
    assert (await decide(dorg, lv["id"], "reject")).status_code == 422
    assert (await decide(dorg, lv["id"], "reject", "Project deadline")).status_code == 200
    d = await internal(dorg, lv["id"])
    assert d["status"] == "rejected" and d["decision_comment"] == "Project deadline"
    assert (await balance(dorg, s, el["id"]))["pending"] == "0.00"
    mine = (await s.worker.get(LEAVE)).json()
    assert [r["status"] for r in mine] == ["rejected"]


async def test_concurrent_requests_for_the_last_day(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    el = await leave_type(dorg)
    await grant(dorg, s, el["id"], "1.00")
    ctx = admin_ctx(dorg)

    async def one(offset: int) -> str:
        day = s.start + timedelta(days=offset)
        try:
            async with tenant_session(dorg.tenant_id) as db:
                await leave_requests.create_request(
                    ctx,
                    db,
                    s.candidate_id,
                    LeaveRequestIn(leave_type_id=uuid.UUID(el["id"]), start_date=day, end_date=day),
                    actor=ActorRef("candidate", s.candidate_id),
                )
            return "ok"
        except Exception as exc:
            code = getattr(exc, "code", None)
            return code if code in ("balance_insufficient",) else f"{type(exc).__name__}: {str(exc)[:400]}"

    results = await asyncio.gather(one(1), one(2))
    assert sorted(results) == ["balance_insufficient", "ok"], results


async def test_proxy_request_and_internal_list(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    unpaid = await leave_type(dorg, paid=False, accrual_mode="none", accrual_amount="0")
    resp = await dorg.admin.client.post(
        f"{TIME}/leave-requests",
        json={
            "candidate_id": str(s.candidate_id),
            "leave_type_id": unpaid["id"],
            "start_date": s.start.isoformat(),
            "end_date": s.start.isoformat(),
            "proxy_reason": "Called in sick",
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["requested_by_kind"] == "user" and resp.json()["proxy_reason"] == "Called in sick"
    listed = (
        await dorg.admin.client.get(f"{TIME}/leave-requests", params={"candidate_id": str(s.candidate_id)})
    ).json()
    assert [r["id"] for r in listed] == [resp.json()["id"]]
    assert (await decide(dorg, resp.json()["id"])).status_code == 200
    wk1 = await _timesheet_for(s, dorg, 0)
    assert _leave_cells(wk1) == [(s.start.isoformat(), "leave_unpaid", "8.00")]


async def test_isolation_worker_b_and_cross_tenant(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    a = await setup(app, dorg)
    b = await setup(app, dorg)
    el = await leave_type(dorg)
    await grant(dorg, a, el["id"], "2.00")
    lv = (await request(a, el["id"], a.start, a.start)).json()
    assert (await b.worker.post(f"{LEAVE}/{lv['id']}/withdraw", headers=csrf())).status_code == 404
    assert (await b.worker.post(f"{LEAVE}/{lv['id']}/cancel", headers=csrf())).status_code == 404
    assert (await b.worker.get(LEAVE)).json() == []
    other = other_dorg.admin.client
    assert (await other.get(f"{TIME}/leave-requests/{lv['id']}")).status_code == 404
    assert (await other.get(f"{TIME}/leave-types/{el['id']}")).status_code == 404
    assert (
        await other.get(f"{TIME}/leave-requests", params={"candidate_id": str(a.candidate_id)})
    ).json() == []
    assert (
        await other.get(f"{TIME}/leave-balances", params={"candidate_id": str(a.candidate_id)})
    ).json() == []
    resp = await other.post(f"{TIME}/leave-requests/{lv['id']}/withdraw", headers=h(other_dorg.admin))
    assert resp.status_code == 404
    # worker portal sees its own balance + types
    bal = (await a.worker.get(f"{WORK.rsplit('/', 1)[0]}/leave-balances")).json()
    assert [(x["leave_type_id"], x["available"]) for x in bal] == [(el["id"], "1.00")]
    types = (await a.worker.get(f"{WORK.rsplit('/', 1)[0]}/leave-types")).json()
    assert el["id"] in [t["id"] for t in types]


async def test_first_timesheet_unchanged_after_submission(app: FastAPI, dorg: DOrg) -> None:
    """Approving leave after the Timesheet was submitted never alters it (prefill only touches drafts)."""
    s = await setup(app, dorg)
    el = await leave_type(dorg)
    await grant(dorg, s, el["id"], "2.00")
    ts = await first_timesheet(s)
    lv = (await request(s, el["id"], s.start, s.start)).json()
    assert (await worker_submit(s, ts["id"], confirm_zero=True)).status_code == 200
    assert (await decide(dorg, lv["id"])).status_code == 200
    assert _leave_cells(await detail(dorg, ts["id"])) == []
