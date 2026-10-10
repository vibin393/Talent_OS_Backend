"""Timesheets end to end (Epic 25): config, Worker entry + validation, OT split, approval route (AM ->
Client ->
Finance), recall / reject / resubmit, offline Client approval, portal isolation, monitor and read
contracts."""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import FastAPI

from app.core.database import tenant_session
from app.schemas.time_portal import ClientTimesheetDetail, ClientTimesheetSummary, WorkerTimesheetOut
from app.services import approvals, time_service
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.time.helpers import (
    AM,
    CLIENT,
    CLIENT_ANY,
    CSRF,
    FINANCE,
    TIME,
    WORK,
    active,
    admin_decide,
    approve_all,
    client_user,
    detail,
    first_timesheet,
    week,
    worker_save,
    worker_submit,
)

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _no_floats(payload: Any) -> None:
    if isinstance(payload, float):
        raise AssertionError(f"float in payload: {payload}")
    if isinstance(payload, dict):
        for v in payload.values():
            _no_floats(v)
    if isinstance(payload, list):
        for v in payload:
            _no_floats(v)


def _totals(d: dict[str, Any]) -> dict[str, str]:
    return {t["category"]: t["hours"] for t in d["totals"]}


async def test_acceptance_flow_ot_split_route_and_loader(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, config={"ot_daily_threshold": "9"})
    cu = await client_user(app, dorg)
    ts = await first_timesheet(s)
    assert ts["status"] == "draft"
    resp = await worker_save(s, ts["id"], week(s.start, "10.50", "8", "8", "8", "8"))
    assert resp.status_code == 200, resp.text
    _no_floats(resp.json())
    preview = (await s.worker.post(f"{WORK}/{ts['id']}/preview-split", headers=CSRF)).json()
    assert {t["category"]: t["hours"] for t in preview["totals"]} == {"regular": "41.00", "ot": "1.50"}
    resp = await worker_submit(s, ts["id"])
    assert resp.status_code == 200, resp.text
    d = await detail(dorg, ts["id"])
    assert d["status"] == "submitted" and _totals(d) == {"regular": "41.00", "ot": "1.50"}
    assert d["current_step"] == "Account Manager"
    _no_floats(d)
    # AM approves -> in_approval; Client approves in the portal; Finance approves
    assert (await admin_decide(dorg, ts["id"])).status_code == 200
    assert (await detail(dorg, ts["id"]))["status"] == "in_approval"
    listed = await cu.client.get(CLIENT)
    assert listed.status_code == 200 and [i["id"] for i in listed.json()] == [ts["id"]]
    assert listed.json()[0]["awaiting_client"] is True
    for forbidden in ("pay_rate", "bill_rate", "margin", "amount"):
        assert forbidden not in listed.text
    resp = await cu.client.post(f"{CLIENT}/{ts['id']}/decide", json={"action": "approve"}, headers=cu.h())
    assert resp.status_code == 200, resp.text
    final = await approve_all(dorg, ts["id"], cu)
    assert final["status"] == "approved" and final["approved_at"]
    steps = [(x["name"], x["outcome"], x["actor_kind"]) for x in final["approval_history"]]
    assert steps == [
        ("Account Manager", "approved", "user"),
        ("Client", "approved", "client_user"),
        ("Finance", "approved", "user"),
    ]
    # loader returns the approved entries (pull-only)
    from app.services.postings import SourceQuery, load_unposted

    async with tenant_session(dorg.tenant_id) as db:
        items = await load_unposted(
            admin_ctx(dorg),
            db,
            SourceQuery(
                ledger="payable", period_end=s.start + timedelta(days=6), placement_ids=[uuid.UUID(s.pid)]
            ),
        )
    entries = [i for i in items if i.source_kind == "timesheet_entry"]
    assert sum((i.quantity for i in entries), Decimal(0)) == Decimal("42.50")
    assert {i.attributes["category"] for i in entries} == {"regular", "ot"}
    assert all(i.attributes["payable"] for i in entries)


async def test_entry_validation_and_editability(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[AM])
    ts = await first_timesheet(s)

    async def code(days: list[dict[str, Any]]) -> list[str]:
        resp = await worker_save(s, ts["id"], days)
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "entry_invalid"
        return [r["code"] for r in resp.json()["error"]["details"]["rows"]]

    assert await code(week(s.start, "7.333")) == ["bad_decimal"]
    assert await code(week(s.start, "0.005")) == ["bad_decimal"]
    assert await code(week(s.start, "7,5")) == ["bad_decimal"]
    assert await code(week(s.start, "24.01")) == ["over_24h"]
    assert await code(
        [{"date": (s.start - timedelta(days=1)).isoformat(), "category": "regular", "hours": "8"}]
    ) == ["outside_period"]
    assert await code(week(s.start, "2", category="ot")) == ["computed_category"]
    ok = await worker_save(s, ts["id"], week(s.start, "24.00"))
    assert ok.status_code == 200, ok.text
    over = await code([{"date": s.start.isoformat(), "category": "holiday", "hours": "1"}])
    assert over == ["over_24h"]
    # empty submit needs confirmation
    ts2 = next(
        t
        for t in (await s.worker.get(WORK)).json()
        if t["period_start"] == (s.start + timedelta(days=7)).isoformat()
    )
    resp = await worker_submit(s, ts2["id"])
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "zero_hours_confirm_required"
    assert (await worker_submit(s, ts2["id"], confirm_zero=True)).status_code == 200
    # submitted -> not editable; recall -> editable again
    assert (await worker_submit(s, ts["id"])).status_code == 200
    resp = await worker_save(s, ts["id"], week(s.start, "8"))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "timesheet_not_editable"
    resp = await s.worker.post(f"{WORK}/{ts['id']}/recall", headers=CSRF)
    assert resp.status_code == 200 and resp.json()["status"] == "draft"
    assert (await worker_submit(s, ts["id"])).status_code == 200
    assert (await admin_decide(dorg, ts["id"])).status_code == 200
    resp = await s.worker.post(f"{WORK}/{ts['id']}/recall", headers=CSRF)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "transition_not_allowed"


async def test_outside_placement_dates_and_days_and_start_end_modes(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[AM], config={"entry_mode": "days", "hours_per_day": "8.00"})
    ts = await first_timesheet(s)
    resp = await worker_save(
        s, ts["id"], [{"date": s.start.isoformat(), "category": "regular", "days": "0.5"}]
    )
    assert resp.status_code == 200, resp.text
    row = next(e for e in resp.json()["entries"] if e["category"] == "regular")
    assert row["hours"] == "4.00"
    # start_end mode (new config version applies to the unsubmitted draft)
    resp = await dorg.admin.client.put(
        f"{TIME}/placements/{s.pid}/config",
        json={"approval_route": [AM], "entry_mode": "start_end"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200 and resp.json()["version"] == 3
    spans = [{"start": f"{x}:00", "end": f"{x}:20"} for x in ("09", "10", "11")]
    resp = await worker_save(
        s, ts["id"], [{"date": s.start.isoformat(), "category": "regular", "spans": spans}]
    )
    assert resp.status_code == 200, resp.text
    assert next(e for e in resp.json()["entries"] if e["category"] == "regular")["hours"] == "1.00"
    history = (
        await dorg.admin.client.get(f"{TIME}/placements/{s.pid}/config", params={"history": "true"})
    ).json()
    assert [c["version"] for c in history] == [1, 2, 3]


async def test_client_reject_requires_comment_then_resubmit_restarts(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[CLIENT_ANY, FINANCE])
    cu = await client_user(app, dorg)
    ts = await first_timesheet(s)
    await worker_save(s, ts["id"], week(s.start, "8", "8"))
    assert (await worker_submit(s, ts["id"])).status_code == 200
    first_request = (await detail(dorg, ts["id"]))["approval_request_id"]
    resp = await cu.client.post(f"{CLIENT}/{ts['id']}/decide", json={"action": "reject"}, headers=cu.h())
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "comment_required"
    resp = await cu.client.post(
        f"{CLIENT}/{ts['id']}/decide",
        json={"action": "reject", "comment": "Tuesday was a holiday"},
        headers=cu.h(),
    )
    assert resp.status_code == 200, resp.text
    mine = (await s.worker.get(f"{WORK}/{ts['id']}")).json()
    assert (
        mine["status"] == "rejected"
        and mine["rejection_comment"] == "Tuesday was a holiday"
        and mine["editable"]
    )
    assert (await worker_save(s, ts["id"], week(s.start, "8", "0"))).status_code == 200
    assert (await worker_submit(s, ts["id"])).status_code == 200
    d = await detail(dorg, ts["id"])
    assert d["status"] == "submitted" and d["approval_request_id"] != first_request
    assert d["current_step"] == "Client"


async def test_same_step_twice_is_409_with_one_decision(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[{"kind": "user", "ref": str(dorg.admin.user_id), "label": "Manager"}])
    ts = await first_timesheet(s)
    await worker_save(s, ts["id"], week(s.start, "8"))
    await worker_submit(s, ts["id"])
    d = await detail(dorg, ts["id"])
    assert (await admin_decide(dorg, ts["id"])).status_code == 200
    again = await dorg.admin.client.post(
        f"/api/v1/approvals/{d['approval_request_id']}/decide",
        json={"action": "approve"},
        headers=h(dorg.admin),
    )
    assert again.status_code == 409
    d = await detail(dorg, ts["id"])
    assert d["status"] == "approved" and len([x for x in d["approval_history"] if x["outcome"]]) == 1


async def test_offline_client_approval_records_evidence(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[CLIENT_ANY])
    await client_user(app, dorg)
    ts = await first_timesheet(s)
    await worker_save(s, ts["id"], week(s.start, "8"))
    await worker_submit(s, ts["id"])
    resp = await dorg.admin.client.post(
        f"{TIME}/timesheets/{ts['id']}/record-client-approval",
        files={"file": ("approval.pdf", PDF, "application/pdf")},
        data={"comment": "Signed sheet received"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved" and body["client_approval_evidence_file_id"]
    assert body["approval_history"][0]["actor_kind"] == "recorded_by_user"


async def test_portal_isolation_and_schemas(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    a = await active(app, dorg, route=[CLIENT_ANY])
    b = await active(app, dorg, route=[CLIENT_ANY])
    ts_a = await first_timesheet(a)
    assert (await b.worker.get(f"{WORK}/{ts_a['id']}")).status_code == 404
    assert (
        await b.worker.put(f"{WORK}/{ts_a['id']}/entries", json={"days": []}, headers=CSRF)
    ).status_code == 404
    # Client of another client -> 404; restricted Client User sees only scoped placements
    from tests.modules.demands.helpers import create_client
    from tests.p2 import make_client_user, sign_in_external

    other_client = await create_client(dorg.admin)
    cu_other = await sign_in_external(
        app, "client_user", await make_client_user(dorg.tenant_id, other_client), dorg.tenant_id, other_client
    )
    assert (await cu_other.client.get(f"{CLIENT}/{ts_a['id']}")).status_code == 404
    restricted = await client_user(app, dorg, restricted=True)
    assert (await restricted.client.get(f"{CLIENT}/{ts_a['id']}")).status_code == 404
    mine = await client_user(app, dorg)
    resp = await mine.client.get(f"{CLIENT}/{ts_a['id']}")
    assert resp.status_code == 200, resp.text
    no_perm = await client_user(app, dorg, permissions=("submissions:review",))
    assert (await no_perm.client.get(CLIENT)).status_code == 403
    # cross-tenant -> 404 on internal endpoints
    assert (await other_dorg.admin.client.get(f"{TIME}/timesheets/{ts_a['id']}")).status_code == 404
    assert (await other_dorg.admin.client.get(f"{TIME}/placements/{a.pid}/config")).status_code == 404
    # field visibility
    worker_fields = set(WorkerTimesheetOut.model_fields)
    assert not {f for f in worker_fields if "rate" in f or "margin" in f or "amount" in f}
    for model in (ClientTimesheetSummary, ClientTimesheetDetail):
        fields = set(model.model_fields)
        assert not {
            f for f in fields if "rate" in f or "margin" in f or "amount" in f or f in ("note", "notes")
        }


async def test_portal_tables_have_org_rls(dorg: DOrg) -> None:
    del dorg
    from sqlalchemy import text

    from app.core.database import owner_engine

    engine = owner_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_policies "
                    "WHERE policyname = 'org_isolation' AND tablename = ANY(:t)"
                ),
                {
                    "t": [
                        "timesheets",
                        "timesheet_entries",
                        "timesheet_adjustment_entries",
                        "timesheet_configs",
                        "placements",
                    ]
                },
            )
        ).scalars()
        found = set(rows)
    await engine.dispose()
    assert found == {
        "timesheets",
        "timesheet_entries",
        "timesheet_adjustment_entries",
        "timesheet_configs",
        "placements",
    }


async def test_monitor_remind_and_read_contracts(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[AM], bill_rate="1234.5678")
    ts = await first_timesheet(s)
    await worker_save(s, ts["id"], [{"date": s.start.isoformat(), "category": "regular", "hours": "7.83"}])
    await worker_submit(s, ts["id"])
    resp = await dorg.admin.client.get(f"{TIME}/monitor")
    assert resp.status_code == 200, resp.text
    _no_floats(resp.json())
    body = resp.json()
    row = next(r for r in body["rows"] if r["timesheet_id"] == ts["id"])
    assert row["estimated_unbilled_value"] == "9666.67"  # 7.83 x 1234.5678 x 1.0000, rounded once
    assert body["masked"] is False and body["totals"][0]["currency"] == "INR"
    missing = [
        r
        for r in body["rows"]
        if r["placement_id"] == s.pid
        and r["status"] == "draft"
        and r["period_end"] < date.today().isoformat()
    ]
    assert missing and all(r["late"] for r in missing)
    resp = await dorg.admin.client.post(
        f"{TIME}/monitor/remind", json={"timesheet_ids": [ts["id"]]}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200 and resp.json()["sent"] == [ts["id"]]
    resp = await dorg.admin.client.post(
        f"{TIME}/monitor/remind", json={"timesheet_ids": [ts["id"]]}, headers=h(dorg.admin)
    )
    assert resp.json()["skipped"][0]["code"] == "reminder_too_soon"
    ctx = admin_ctx(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        state = await time_service.timesheet_state_for(ctx, db, uuid.UUID(s.pid), s.start + timedelta(days=2))
        assert state is not None and state.status == "submitted" and state.locked is False
        p = (
            await db.execute(
                __import__("sqlalchemy").text("SELECT legal_entity_id FROM placements WHERE id = :i"),
                {"i": uuid.UUID(s.pid)},
            )
        ).scalar_one()
        pending = await time_service.unapproved_timesheets(ctx, db, p, s.start, s.start + timedelta(days=6))
        assert [str(x.timesheet_id) for x in pending] == [ts["id"]]
        steps = await approvals.resolve_route(
            ctx, db, "placement_time_route", placement_id=uuid.UUID(s.pid), include_client=False
        )
        assert [x.approver_kind for x in steps] == ["placement_account_manager"]
        pay = await time_service.pay_inputs(ctx, db, uuid.UUID(s.pid), s.start, s.start + timedelta(days=6))
        bill = await time_service.billing_inputs(
            ctx, db, uuid.UUID(s.pid), s.start, s.start + timedelta(days=6)
        )
        assert pay.working_days == 5 and pay.hourly_equivalent is None
        assert bill.bill_multipliers["ot"] == Decimal("1.5000") and "holiday" not in bill.billable_categories
        from app.services.placements_service import offboarding_verifiers

        result = await offboarding_verifiers()["final_timesheet_approved"](
            db, uuid.UUID(s.pid), s.start + timedelta(days=2)
        )
        assert result.done is False


async def test_monthly_rate_hourly_equivalent_and_proration(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[AM], rate_unit="month", pay_rate="88000.00", bill_rate="120000.00")
    ctx = admin_ctx(dorg)
    first = date(s.start.year, s.start.month, 1)
    async with tenant_session(dorg.tenant_id) as db:
        pay = await time_service.pay_inputs(ctx, db, uuid.UUID(s.pid), first, first + timedelta(days=40))
    from app.services.time import calendar

    month = calendar.month_working_days(first)
    assert pay.month_working_days == month
    assert pay.hourly_equivalent == Decimal("88000.00") / (Decimal(month) * Decimal("8.00"))
    assert pay.proration_factor == calendar.proration_factor(max(first, s.start), first + timedelta(days=40))


async def test_copy_previous_and_prefill_registry(app: FastAPI, dorg: DOrg) -> None:
    from app.schemas.time import PrefillItem
    from app.services.time import prefill

    s = await active(app, dorg, route=[AM], weeks_ago=3)

    async def fake_holidays(db: Any, placement_id: uuid.UUID, start: date, end: date) -> list[PrefillItem]:
        del db
        festival = s.start + timedelta(days=9)  # Wednesday of the second week
        return (
            [
                PrefillItem(
                    date=festival,
                    category="holiday",
                    hours=Decimal("8.00"),
                    label="Festival",
                )
            ]
            if str(placement_id) == s.pid and start <= festival <= end
            else []
        )

    prefill.register_prefill_source("holidays", fake_holidays)
    try:
        ts = await first_timesheet(s)
        await worker_save(s, ts["id"], week(s.start, "8", "7.5"))
        nxt = next(
            t
            for t in (await s.worker.get(WORK)).json()
            if t["period_start"] == (s.start + timedelta(days=7)).isoformat()
        )
        resp = await s.worker.post(f"{WORK}/{nxt['id']}/refresh-prefill", headers=CSRF)
        assert resp.status_code == 200, resp.text
        pre = [e for e in resp.json()["entries"] if e["prefilled"]]
        assert [(e["category"], e["hours"], e["deletable"]) for e in pre] == [("holiday", "8.00", False)]
        resp = await worker_save(
            s, nxt["id"], [{"date": pre[0]["entry_date"], "category": "holiday", "hours": "0"}]
        )
        assert (
            resp.status_code == 422 and resp.json()["error"]["details"]["rows"][0]["code"] == "prefill_locked"
        )
        resp = await s.worker.post(f"{WORK}/{nxt['id']}/copy-previous", headers=CSRF)
        assert resp.status_code == 200, resp.text
        regular = sorted(
            (e["entry_date"], e["hours"]) for e in resp.json()["entries"] if e["category"] == "regular"
        )
        assert [hv for _, hv in regular] == ["8.00", "7.50"]
        async with tenant_session(dorg.tenant_id) as db:
            pay = await time_service.pay_inputs(
                admin_ctx(dorg),
                db,
                uuid.UUID(s.pid),
                s.start + timedelta(days=7),
                s.start + timedelta(days=13),
            )
        assert pay.working_days == 4  # the prefilled holiday is not a working day
    finally:
        prefill.unregister_prefill_source("holidays")
