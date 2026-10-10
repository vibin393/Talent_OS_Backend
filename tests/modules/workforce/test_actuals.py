# ruff: noqa: E501
"""Epic 44 story 44.4 (FR-225): plan vs actual, Decimal committed cost, masking, export; pure cost arithmetic."""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import FastAPI

from app.core.database import tenant_session
from app.models.placements import RateSegment
from app.services.workforce import cost
from tests.modules.commercials.helpers import COrg
from tests.modules.identity.conftest import csrf, join
from tests.modules.workforce.helpers import (
    WF,
    activate_placement,
    approved_plan,
    candidate,
    ok,
    open_demand,
    slots,
)


# ================================================================ pure
def test_months_remaining_and_rates() -> None:
    assert cost.months_remaining(date(2027, 5, 1), date(2028, 3, 31)) == Decimal("11.0000")
    assert cost.months_remaining(date(2027, 5, 16), date(2027, 5, 31)) == Decimal("0.5161")
    assert cost.months_remaining(date(2028, 5, 1), date(2028, 3, 31)) == 0
    hours, days = Decimal(168), Decimal(21)
    assert cost.annualise(Decimal("100000"), "month", hours, days) == Decimal("1200000")
    assert cost.monthly(Decimal("1000"), "hour", hours, days) == Decimal("168000")
    assert cost.monthly(Decimal("5000"), "day", hours, days) == Decimal("105000")
    assert cost.monthly(Decimal("9"), "fixed", hours, days) is None
    assert cost.permanent_commitment(Decimal("1200000"), Decimal("11")) == Decimal("1100000.00")
    assert cost.contract_commitment(Decimal("168000"), Decimal("0.5161")) == Decimal("86704.80")
    assert isinstance(cost.convert(Decimal("10"), Decimal("83.123456")), Decimal)


# ================================================================ API
async def _contract_slot(corg: COrg) -> tuple[dict, str]:
    p = await approved_plan(
        corg, client_id=str(corg.client_id), lines=[{"headcount": 3, "budget_amount": "3000000.00"}]
    )
    s = await slots(corg, p["id"])
    conv = await ok(
        await corg.c.post(
            f"{WF}/slots/convert", json={"slot_ids": [s[0]["id"], s[1]["id"]]}, headers=corg.h()
        )
    )
    for c in conv["converted"]:
        await open_demand(corg, c["demand_id"])
    demand_id = conv["converted"][0]["demand_id"]
    pid = await activate_placement(corg, uuid.UUID(demand_id), await candidate(corg))
    async with tenant_session(corg.tenant_id) as db:
        db.add(
            RateSegment(
                id=uuid.uuid4(), tenant_id=corg.tenant_id, placement_id=pid, client_id=corg.client_id,
                effective_from=date(2027, 5, 1), rate_unit="hour", currency="INR", pay_rate=Decimal("1000.0000"),
                status="active",
            )
        )  # fmt: skip
    return p, demand_id


async def test_plan_vs_actual_committed_cost(corg: COrg) -> None:
    p, demand_id = await _contract_slot(corg)
    out = await ok(await corg.c.get(f"{WF}/plans/{p['id']}/actuals", headers=corg.h()))
    assert out["money_masked"] is False and out["currency"] == "INR" and out["effective_revision"] == 0
    (row,) = out["rows"]
    assert (row["planned"], row["filled"], row["requisitioned"], row["open"]) == (3, 1, 1, 1)
    # 1000/h x 168 h x 11 months (May 2027 .. Mar 2028)
    assert (
        row["committed"] == "1848000.00" and row["budget"] == "3000000.00" and row["variance"] == "1152000.00"
    )
    assert row["pending_commitment"] is True  # the second Demand has no Offer / Placement yet
    assert demand_id in row["demand_ids"]
    by_month = await ok(await corg.c.get(f"{WF}/plans/{p['id']}/actuals?group_by=month", headers=corg.h()))
    assert len(by_month["rows"]) == 12 and by_month["totals"]["committed"] == "1848000.00"
    may = next(r for r in by_month["rows"] if r["month"] == "2027-05")
    assert may["planned"] == 3
    xlsx = await corg.c.get(f"{WF}/plans/{p['id']}/actuals/export", headers=corg.h())
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"


async def test_money_masked_without_compensation_view(app: FastAPI, corg: COrg) -> None:
    p, _ = await _contract_slot(corg)
    fin = await join(
        app, corg.tenant, "finance_admin", name="Fin Admin"
    )  # workforce_plan:read, no compensation:view
    try:
        out = await ok(await fin.client.get(f"{WF}/plans/{p['id']}/actuals", headers=csrf(fin.client)))
        assert out["money_masked"] is True
        assert all(out["rows"][0][k] is None for k in ("budget", "committed", "variance"))
        assert out["rows"][0]["filled"] == 1  # counts stay visible
        plan_out = await ok(await fin.client.get(f"{WF}/plans/{p['id']}", headers=csrf(fin.client)))
        assert plan_out["total_budget"] is None and plan_out["money_masked"] is True
        (line,) = await ok(await fin.client.get(f"{WF}/plans/{p['id']}/lines", headers=csrf(fin.client)))
        assert line["budget_amount"] is None
    finally:
        await fin.client.aclose()


async def test_plan_summary(corg: COrg) -> None:
    from app.services import workforce_service

    await approved_plan(corg, lines=[{"headcount": 4}])
    async with tenant_session(corg.tenant_id) as db:
        out = await workforce_service.plan_summary(corg.ctx(), db, date(2027, 1, 1), date(2027, 12, 31))
    assert out.plans_by_status == {"approved": 1} and out.approved_headcount == 4 and out.open_slots == 4
