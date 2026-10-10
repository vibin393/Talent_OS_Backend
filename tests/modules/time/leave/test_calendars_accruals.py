"""Holiday calendars (resolution, copy, optional allowance, prefill), accruals exactly-once, year end, ledger
consistency, Client leave calendar opt-in / field visibility, erasure claim."""

import asyncio
import random
import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.database import tenant_session
from app.models.time import LeaveBalanceEntry
from app.schemas.leave import AdjustBalanceIn
from app.services.time import holidays, leave_balances
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.time.helpers import TIME, client_user, detail, first_timesheet
from tests.modules.time.leave.helpers import WORK, csrf, decide, grant, leave_type, request, setup

CAL = f"{TIME}/holiday-calendars"


async def _calendar(org: DOrg, **body: object) -> dict:
    resp = await org.admin.client.post(CAL, json=body, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_resolution_precedence_and_prefill(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    y = s.start.year
    tue = s.start + timedelta(days=1)
    country = await _calendar(
        dorg,
        name="India",
        year=y,
        scope_kind="country",
        holidays=[{"holiday_date": tue.isoformat(), "name": "National Day"}],
    )
    async with tenant_session(dorg.tenant_id) as db:
        assert await holidays.default_calendar_for(admin_ctx(dorg), db, uuid.UUID(s.pid), y) == uuid.UUID(
            country["id"]
        )
    le = await _calendar(
        dorg, name="LE", year=y, scope_kind="legal_entity", legal_entity_id=s.placement["legal_entity_id"]
    )
    async with tenant_session(dorg.tenant_id) as db:
        assert await holidays.default_calendar_for(admin_ctx(dorg), db, uuid.UUID(s.pid), y) == uuid.UUID(
            le["id"]
        )
    wed = s.start + timedelta(days=2)
    cl = await _calendar(
        dorg,
        name="Client",
        year=y,
        scope_kind="client",
        client_id=str(dorg.client_id),
        holidays=[{"holiday_date": wed.isoformat(), "name": "Client Day", "type": "client"}],
    )
    resp = await dorg.admin.client.get(
        f"{TIME}/placements/{s.pid}/holidays",
        params={"from": s.start.isoformat(), "to": (s.start + timedelta(days=6)).isoformat()},
    )
    assert resp.status_code == 200 and [x["name"] for x in resp.json()["holidays"]] == ["Client Day"]
    assert resp.json()["calendar_ids"] == [cl["id"]]
    # the calendar change re-prefills the draft timesheet (holiday 8.00 h on Wednesday)
    ts = await detail(dorg, (await first_timesheet(s))["id"])
    assert [
        (e["entry_date"], e["category"], e["hours"]) for e in ts["entries"] if e["category"] == "holiday"
    ] == [(wed.isoformat(), "holiday", "8.00")]
    # a request spanning the holiday excludes it
    el = await leave_type(dorg)
    await grant(dorg, s, el["id"], "5.00")
    resp = await s.worker.get(
        f"{WORK}/leave-requests/preview",
        params={
            "leave_type_id": el["id"],
            "start_date": s.start.isoformat(),
            "end_date": (s.start + timedelta(days=4)).isoformat(),
        },
    )
    assert resp.json()["days"] == "4.00"
    # duplicate scope + year -> 409
    resp = await dorg.admin.client.post(
        CAL, json={"name": "x", "year": y, "scope_kind": "country"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 409


async def test_copy_skips_feb_29_and_flags_review(app: FastAPI, dorg: DOrg) -> None:
    src = await _calendar(
        dorg,
        name="India 2028",
        year=2028,
        scope_kind="state",
        state_code="29",
        holidays=[
            {"holiday_date": "2028-02-29", "name": "Leap Day"},
            {"holiday_date": "2028-01-26", "name": "Republic Day"},
            {"holiday_date": "2028-03-10", "name": "Optional Fest", "type": "optional"},
        ],
    )
    resp = await dorg.admin.client.post(f"{CAL}/{src['id']}/copy", json={"year": 2029}, headers=h(dorg.admin))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert [x["name"] for x in out["skipped"]] == ["Leap Day"]
    assert {x["name"]: x["needs_review"] for x in out["copied"]} == {
        "Republic Day": True,
        "Optional Fest": False,
    }
    assert out["calendar"]["name"] == "India 2029" and out["calendar"]["year"] == 2029
    # unassigned -> deleted
    resp = await dorg.admin.client.delete(f"{CAL}/{out['calendar']['id']}", headers=h(dorg.admin))
    assert resp.json() == {"status": "deleted"}


async def test_optional_allowance_and_submitted_guard(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    y = s.start.year
    d1, d2 = s.start + timedelta(days=1), s.start + timedelta(days=8)
    cal = await _calendar(
        dorg,
        name="Opt",
        year=y,
        scope_kind="country",
        optional_allowance=1,
        holidays=[
            {"holiday_date": d1.isoformat(), "name": "Opt A", "type": "optional"},
            {"holiday_date": d2.isoformat(), "name": "Opt B", "type": "optional"},
        ],
    )
    hols = {
        x["name"]: x["id"] for x in (await dorg.admin.client.get(f"{CAL}/{cal['id']}")).json()["holidays"]
    }
    resp = await s.worker.post(
        f"{WORK}/optional-holidays", json={"holiday_id": hols["Opt B"]}, headers=csrf()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["optional_selected"] == 1
    resp = await s.worker.post(
        f"{WORK}/optional-holidays", json={"holiday_id": hols["Opt A"]}, headers=csrf()
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "optional_allowance_exceeded"
    # submitted timesheet covering the date -> timesheet_submitted
    ts = await first_timesheet(s)
    assert (
        await s.worker.post(
            f"/api/v1/portal/work/timesheets/{ts['id']}/submit", json={"confirm_zero": True}, headers=csrf()
        )
    ).status_code == 200
    resp = await s.worker.post(
        f"{WORK}/optional-holidays", json={"holiday_id": hols["Opt B"], "selected": False}, headers=csrf()
    )
    assert resp.status_code == 200  # Opt B is in week 2 (still draft)
    resp = await s.worker.post(
        f"{WORK}/optional-holidays", json={"holiday_id": hols["Opt A"]}, headers=csrf()
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "timesheet_submitted"


async def test_accrual_exactly_once_and_year_end(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    await leave_type(dorg)
    month = s.start.replace(day=1)
    ctx = admin_ctx(dorg)
    for _ in range(2):
        resp = await dorg.admin.client.post(
            f"{TIME}/leave-accruals/run", json={"month": month.isoformat()}, headers=h(dorg.admin)
        )
        assert resp.status_code == 200, resp.text

    async def run_once() -> None:
        async with tenant_session(dorg.tenant_id) as db:
            await leave_balances.run_monthly_accruals(ctx, db, month)

    await asyncio.gather(*(run_once() for _ in range(20)))
    async with tenant_session(dorg.tenant_id) as db:
        n = (
            await db.execute(
                select(func.count())
                .select_from(LeaveBalanceEntry)
                .where(
                    LeaveBalanceEntry.candidate_id == s.candidate_id,
                    LeaveBalanceEntry.entry_kind == "accrual",
                )
            )
        ).scalar_one()
        assert n == 1
        assert (await leave_balances.verify_balances(ctx, db)).mismatches == []
    # year end: carry limit 10.00 on 13.25 -> carry 10.00, lapse -3.25
    capped = await leave_type(dorg, accrual_mode="none", accrual_amount="0", carry_forward_limit="10.00")
    await grant(dorg, s, capped["id"], "13.25", year="2030")
    async with tenant_session(dorg.tenant_id) as db:
        await leave_balances.run_year_end(ctx, db, "2030")
        await leave_balances.run_year_end(ctx, db, "2030")  # idempotent
        rows = (
            await db.execute(
                select(
                    LeaveBalanceEntry.entry_kind, LeaveBalanceEntry.days, LeaveBalanceEntry.leave_year
                ).where(
                    LeaveBalanceEntry.leave_type_id == uuid.UUID(capped["id"]),
                    LeaveBalanceEntry.entry_kind.in_(("lapse", "carry_forward")),
                )
            )
        ).all()
    assert sorted((k, str(d), y) for k, d, y in rows) == [
        ("carry_forward", "10.00", "2031"),
        ("lapse", "-3.25", "2030"),
    ]


async def test_ledger_sum_matches_cache_after_random_operations(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    el = await leave_type(dorg, allow_negative=True)
    ctx = admin_ctx(dorg)
    rng = random.Random(26)
    for _ in range(12):
        days = Decimal(rng.choice(["1.00", "-0.50", "2.25", "-1.75", "0.25"]))
        async with tenant_session(dorg.tenant_id) as db:
            await leave_balances.adjust(
                ctx,
                db,
                AdjustBalanceIn(
                    candidate_id=s.candidate_id,
                    leave_type_id=uuid.UUID(el["id"]),
                    legal_entity_id=uuid.UUID(s.placement["legal_entity_id"]),
                    days=str(days),
                    reason="r",
                    leave_year=str(s.start.year),
                ),
            )
    lv = (await request(s, el["id"], s.start, s.start)).json()
    assert (await decide(dorg, lv["id"])).status_code == 200
    async with tenant_session(dorg.tenant_id) as db:
        assert (await leave_balances.verify_balances(ctx, db)).mismatches == []


async def test_client_calendar_opt_in_and_field_visibility(app: FastAPI, dorg: DOrg) -> None:
    s = await setup(app, dorg)
    el = await leave_type(dorg, name="Secret Sabbatical")
    await grant(dorg, s, el["id"], "2.00")
    lv = (await request(s, el["id"], s.start, s.start, reason="private reason")).json()
    assert (await decide(dorg, lv["id"])).status_code == 200
    cu = await client_user(app, dorg)
    params = {"from": s.start.isoformat(), "to": (s.start + timedelta(days=6)).isoformat()}
    assert (await cu.client.get("/api/v1/client-portal/leave-calendar", params=params)).status_code == 404
    resp = await dorg.admin.client.put(
        f"{TIME}/leave-client-settings/{dorg.client_id}",
        json={"leave_client_mode": "none", "client_team_calendar": True},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200 and resp.json()["team_calendar_enabled"] is True
    resp = await cu.client.get("/api/v1/client-portal/leave-calendar", params=params)
    assert resp.status_code == 200, resp.text
    assert [d["label"] for r in resp.json()["rows"] for d in r["days"]] == ["On leave"]
    for forbidden in ("Secret Sabbatical", "private reason", "balance", "leave_type"):
        assert forbidden not in resp.text
    restricted = await client_user(app, dorg, restricted=True)
    resp = await restricted.client.get("/api/v1/client-portal/leave-calendar", params=params)
    assert resp.status_code == 200 and resp.json()["rows"] == []
    # internal team calendar shows the type
    resp = await dorg.admin.client.get(
        f"{TIME}/leave-calendar", params={**params, "client_id": str(dorg.client_id)}
    )
    assert (
        resp.status_code == 200
        and resp.json()["workers"][0]["days"][0]["leave_type_name"] == "Secret Sabbatical"
    )


def test_leave_is_not_a_posting_source_and_tables_are_claimed() -> None:
    from app.services import load_erasure_handlers
    from app.services.postings import posting_sources
    from app.services.privacy import registry

    load_erasure_handlers()
    claimed = registry.claimed_tables()
    for t in ("leave_requests", "leave_balance_entries", "leave_balances", "worker_optional_holidays"):
        assert t in claimed
    assert not [k for k in posting_sources() if "leave" in k]
