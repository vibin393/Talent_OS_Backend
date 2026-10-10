"""Offboarding cases, conversion, early termination, end-date alerts and end-of-term cases."""

import asyncio
import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.events import subscribe, unsubscribe
from app.events.placements import PlacementConverted, PlacementEnded
from app.services.placements import end_alerts
from app.services.placements.exit_events import PlacementEndingSoon
from tests.modules.demands.helpers import DOrg
from tests.modules.placements_exit.helpers import (
    API,
    PL,
    SYSTEM_KINDS,
    active,
    case_detail,
    complete_manual,
    fake_verifiers,
    placement_row,
    post,
    sql,
)
from tests.p2 import make_client_user, sign_in_external


def _lwd(days: int = 10) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


async def _start(org: DOrg, pid: str, reason: str = "client_termination", days: int = 10) -> Any:
    return await post(
        org, f"/placements/{pid}/offboarding", {"reason": reason, "last_working_day": _lwd(days)}
    )


async def test_initiate_materialises_template(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    p = await active(dorg)
    old_end = (await placement_row(dorg, p["id"]))["end_date"]
    with fake_verifiers(monkeypatch, dict.fromkeys(SYSTEM_KINDS, False)):
        resp = await _start(dorg, p["id"])
    assert resp.status_code == 201, resp.text
    case = resp.json()
    assert case["status"] == "initiated" and case["human_id"].startswith("OFF")
    assert case["previous_end_date"] == old_end.isoformat()
    kinds = [i["kind"] for i in case["items"]]
    assert len(kinds) == 10 and "client_confirmation" in kinds and "exit_feedback" in kinds
    assert all(i["owner_id"] == str(dorg.admin.user_id) for i in case["items"])
    fin = next(i for i in case["items"] if i["kind"] == "final_invoice")
    assert fin["due_date"] == (date.today() + timedelta(days=20)).isoformat()
    row = await placement_row(dorg, p["id"])
    assert row["status"] == "offboarding" and row["end_date"].isoformat() == _lwd()
    tasks = await sql(dorg, "SELECT id FROM tasks WHERE source = 'offboarding'")
    assert len(tasks) == 10
    again = await _start(dorg, p["id"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "offboarding_in_progress"
    ext = await post(dorg, f"/placements/{p['id']}/extensions", {"new_end_date": _lwd(200)})
    assert ext.status_code == 409


async def test_initiate_withdraws_open_extension(dorg: DOrg) -> None:
    p = await active(dorg)
    ext = (await post(dorg, f"/placements/{p['id']}/extensions", {"new_end_date": _lwd(200)})).json()
    assert (await _start(dorg, p["id"])).status_code == 201
    got = (await dorg.admin.client.get(f"{API}/extensions/{ext['id']}")).json()
    assert got["status"] == "withdrawn"


async def test_refresh_toggles_and_close_flow(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    p = await active(dorg)
    state = dict.fromkeys(SYSTEM_KINDS, False)
    seen: list[Any] = []
    subscribe(PlacementEnded, seen.append)
    try:
        with fake_verifiers(monkeypatch, state):
            case = (await _start(dorg, p["id"], reason="end_of_term")).json()
            case = await complete_manual(dorg, case)
            closed = await post(dorg, f"/offboarding-cases/{case['id']}/close")
            assert closed.status_code == 409 and closed.json()["error"]["code"] == "items_incomplete"
            state.update(dict.fromkeys(SYSTEM_KINDS, True))
            case = (await post(dorg, f"/offboarding-cases/{case['id']}/refresh")).json()
            assert case["status"] == "ready_to_close", case
            assert all(i["auto_verified"] for i in case["items"] if i["kind"] in SYSTEM_KINDS)
            state["final_invoice"] = False  # regression before close
            case = (await post(dorg, f"/offboarding-cases/{case['id']}/refresh")).json()
            assert case["status"] == "in_progress"
            assert next(i for i in case["items"] if i["kind"] == "final_invoice")["status"] == "pending"
            state["final_invoice"] = True
            await post(dorg, f"/offboarding-cases/{case['id']}/refresh")
            resp = await post(dorg, f"/offboarding-cases/{case['id']}/close")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "closed" and resp.json()["bench_eligible"] is True
    finally:
        unsubscribe(PlacementEnded, seen.append)
    assert (await placement_row(dorg, p["id"]))["status"] == "ended"
    assert len(seen) == 1 and seen[0].bench_eligible is True and seen[0].reason == "end_of_term"


@pytest.mark.parametrize(
    ("worker_type", "reason", "eligible"),
    [
        ("employee", "client_termination", True),
        ("contractor", "end_of_term", False),
        ("employee", "misconduct", False),
    ],
)
async def test_close_bench_eligibility(
    dorg: DOrg, monkeypatch: pytest.MonkeyPatch, worker_type: str, reason: str, eligible: bool
) -> None:
    p = await active(dorg, worker_type=worker_type)
    seen: list[Any] = []
    subscribe(PlacementEnded, seen.append)
    try:
        with fake_verifiers(monkeypatch, dict.fromkeys(SYSTEM_KINDS, True)):
            case = (await _start(dorg, p["id"], reason=reason)).json()
            await complete_manual(dorg, case)
            resp = await post(dorg, f"/offboarding-cases/{case['id']}/close")
            assert resp.status_code == 200, resp.text
    finally:
        unsubscribe(PlacementEnded, seen.append)
    assert [e.bench_eligible for e in seen] == [eligible]
    assert seen[0].worker_type == worker_type


async def test_waive_needs_reason(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    p = await active(dorg)
    with fake_verifiers(monkeypatch, dict.fromkeys(SYSTEM_KINDS, False)):
        case = (await _start(dorg, p["id"])).json()
    item = next(i for i in case["items"] if i["kind"] == "equipment_return")
    assert (await post(dorg, f"/offboarding-items/{item['id']}/waive", {"reason": ""})).status_code == 422
    resp = await post(dorg, f"/offboarding-items/{item['id']}/waive", {"reason": "No equipment issued"})
    assert resp.status_code == 200, resp.text
    assert next(i for i in resp.json()["items"] if i["id"] == item["id"])["status"] == "waived"


async def test_cancel_restores_end_date(dorg: DOrg) -> None:
    p = await active(dorg)
    old_end = (await placement_row(dorg, p["id"]))["end_date"]
    case = (await _start(dorg, p["id"])).json()
    resp = await post(
        dorg, f"/offboarding-cases/{case['id']}/cancel", {"reason": "Client retained the worker"}
    )
    assert resp.status_code == 200, resp.text
    row = await placement_row(dorg, p["id"])
    assert row["status"] == "active" and row["end_date"] == old_end
    hist = await sql(
        dorg,
        "SELECT cause FROM placement_end_date_history WHERE placement_id = :i ORDER BY at",
        i=uuid.UUID(p["id"]),
    )
    assert [h["cause"] for h in hist] == ["offboarding", "cancel_offboarding"]


async def test_cancel_after_last_working_day_409(dorg: DOrg) -> None:
    p = await active(dorg)
    case = (
        await post(
            dorg,
            f"/placements/{p['id']}/offboarding",
            {"reason": "other", "last_working_day": date.today().isoformat()},
        )
    ).json()
    resp = await post(dorg, f"/offboarding-cases/{case['id']}/cancel", {"reason": "Too late"})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "past_last_working_day"


async def test_concurrent_initiate_one_case(dorg: DOrg) -> None:
    p = await active(dorg)
    results = await asyncio.gather(*(_start(dorg, p["id"]) for _ in range(4)))
    assert sorted(r.status_code for r in results).count(201) == 1
    cases = await sql(dorg, "SELECT id FROM offboarding_cases WHERE placement_id = :i", i=uuid.UUID(p["id"]))
    assert len(cases) == 1


async def test_conversion_idempotent_no_bench(dorg: DOrg) -> None:
    p = await active(dorg)
    seen: list[Any] = []
    subscribe(PlacementConverted, seen.append)
    key = {"Idempotency-Key": f"conv-{uuid.uuid4().hex}"}
    body = {"conversion_date": _lwd(15), "offered_compensation": "1200000.00"}
    try:
        first = await post(dorg, f"/placements/{p['id']}/conversion", body, **key)
        second = await post(dorg, f"/placements/{p['id']}/conversion", body, **key)
    finally:
        unsubscribe(PlacementConverted, seen.append)
    assert first.status_code == 201, first.text
    assert second.status_code == 201 and second.json()["id"] == first.json()["id"]
    assert len(seen) == 1 and seen[0].fee_amount is None  # no contract -> no fee rule
    rows = await sql(
        dorg,
        "SELECT reason, bench_eligible FROM offboarding_cases WHERE placement_id = :i",
        i=uuid.UUID(p["id"]),
    )
    assert rows == [{"reason": "converted", "bench_eligible": False}]
    third = await post(dorg, f"/placements/{p['id']}/conversion", body, **{"Idempotency-Key": "other-key"})
    assert third.status_code == 409


async def test_conversion_fee_from_contract_rule(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.placements import conversion

    async def rule(*_: Any) -> dict[str, Any]:
        return {"kind": "pct_of_ctc", "value": "8.33", "waive_after_months": None}

    monkeypatch.setattr(conversion, "fee_rule", rule)
    p = await active(dorg)
    prev = await post(
        dorg, f"/placements/{p['id']}/conversion/fee-preview", {"offered_compensation": "1200000.00"}
    )
    assert prev.status_code == 200 and prev.json()["fee_proposal"] == "99960.00", prev.text
    resp = await post(
        dorg,
        f"/placements/{p['id']}/conversion",
        {"conversion_date": _lwd(5), "offered_compensation": "1200000.00"},
        **{"Idempotency-Key": uuid.uuid4().hex},
    )
    assert resp.status_code == 201 and resp.json()["fee_proposal"] == "99960.00"


async def test_early_termination_shortfall_and_attrition(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.placements import early_termination

    async def npd(*_: Any) -> int:
        return 30

    monkeypatch.setattr(early_termination, "notice_period_days", npd)
    start = date.today() - timedelta(days=20)
    p = await active(dorg, start=start)
    notice = date.today()
    resp = await post(
        dorg,
        f"/placements/{p['id']}/early-termination",
        {
            "initiated_by": "worker",
            "notice_date": notice.isoformat(),
            "requested_lwd": (notice + timedelta(days=9)).isoformat(),
            "reason": "Relocating",
        },
    )
    assert resp.status_code == 201, resp.text
    case = resp.json()
    assert case["reason"] == "worker_resignation" and case["notice_shortfall_days"] == 21
    assert case["notice_period_days_snapshot"] == 30 and case["early_attrition"] is True


async def test_client_confirms_offboarding(app: FastAPI, dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    p = await active(dorg)
    with fake_verifiers(monkeypatch, dict.fromkeys(SYSTEM_KINDS, False)):
        case = (await _start(dorg, p["id"])).json()
        cu = await make_client_user(dorg.tenant_id, dorg.client_id, permissions=("placements:view",))
        client = await sign_in_external(app, "client_user", cu, dorg.tenant_id, dorg.client_id)
        rows = (await client.client.get(f"{API}/client-portal/placements/offboarding")).json()
        assert [r["id"] for r in rows] == [case["id"]]
        resp = await client.client.post(
            f"{API}/client-portal/placements/{p['id']}/offboarding/confirm",
            json={"last_working_day_ack": True},
            headers=client.h(),
        )
        assert resp.status_code == 200, resp.text
        detail = await case_detail(dorg, case["id"])
    item = next(i for i in detail["items"] if i["kind"] == "client_confirmation")
    assert item["status"] == "done" and item["completed_by_kind"] == "client_user"


async def test_offboarding_secure_link(dorg: DOrg) -> None:
    p = await active(dorg)
    case = (await _start(dorg, p["id"])).json()
    token = (
        (await post(dorg, f"/offboarding-cases/{case['id']}/client-link")).json()["url"].rsplit("/", 1)[-1]
    )
    view = await dorg.admin.client.get(f"{API}/public/links/{token}/offboarding")
    assert view.status_code == 200 and view.json()["client_confirmed"] is False
    ok = await dorg.admin.client.post(
        f"{API}/public/links/{token}/offboarding", json={"last_working_day_ack": True}
    )
    assert ok.status_code == 200, ok.text
    again = await dorg.admin.client.post(
        f"{API}/public/links/{token}/offboarding", json={"last_working_day_ack": True}
    )
    assert again.status_code == 410


async def test_cross_tenant_404(dorg: DOrg, other_dorg: DOrg) -> None:
    p = await active(dorg)
    case = (await _start(dorg, p["id"])).json()
    o = other_dorg.admin.client
    assert (await o.get(f"{API}/offboarding-cases/{case['id']}")).status_code == 404
    assert (await o.get(f"{PL}/{p['id']}/offboarding")).status_code == 404
    assert (await post(other_dorg, f"/offboarding-cases/{case['id']}/close")).status_code == 404
    assert (
        await post(other_dorg, f"/offboarding-items/{case['items'][0]['id']}/waive", {"reason": "x"})
    ).status_code == 404
    assert (
        await post(
            other_dorg,
            f"/placements/{p['id']}/conversion",
            {"conversion_date": _lwd(), "offered_compensation": "1"},
            **{"Idempotency-Key": uuid.uuid4().hex},
        )
    ).status_code == 404


# ---------------------------------------------------------------- end-date alerts / end of term
def _ctx(org: DOrg) -> RequestContext:
    return RequestContext(tenant_id=org.tenant_id, actor_kind="system")


async def _alerts(org: DOrg, today: date) -> int:
    async with tenant_session(org.tenant_id) as db:
        return await end_alerts.run_alerts(_ctx(org), db, today)


async def test_alert_sequence_each_offset_once(dorg: DOrg) -> None:
    end = date.today() + timedelta(days=61)
    p = await active(dorg, end=end)
    seen: list[Any] = []
    subscribe(PlacementEndingSoon, seen.append)
    try:
        for days_left in (61, 60, 60, 45, 30, 29, 15):
            await _alerts(dorg, end - timedelta(days=days_left))
    finally:
        unsubscribe(PlacementEndingSoon, seen.append)
    mine = [e.offset_days for e in seen if str(e.placement_id) == p["id"]]
    assert mine == [60, 30, 15]
    # a changed end date starts a fresh series
    new_end = end + timedelta(days=30)
    await sql(dorg, "UPDATE placements SET end_date = :e WHERE id = :i", e=new_end, i=uuid.UUID(p["id"]))
    await _alerts(dorg, new_end - timedelta(days=60))
    rows = await sql(
        dorg,
        "SELECT offset_days FROM placement_end_alerts WHERE placement_id = :i AND end_date = :e",
        i=uuid.UUID(p["id"]),
        e=new_end,
    )
    assert [r["offset_days"] for r in rows] == [60]


async def test_extension_suppresses_later_alerts(dorg: DOrg) -> None:
    end = date.today() + timedelta(days=45)
    p = await active(dorg, end=end)
    await _alerts(dorg, end - timedelta(days=60))
    assert (
        await post(dorg, f"/placements/{p['id']}/extensions", {"new_end_date": _lwd(300)})
    ).status_code == 201
    await _alerts(dorg, end - timedelta(days=30))
    await _alerts(dorg, end - timedelta(days=15))
    rows = await sql(
        dorg, "SELECT offset_days FROM placement_end_alerts WHERE placement_id = :i", i=uuid.UUID(p["id"])
    )
    assert [r["offset_days"] for r in rows] == [60]


async def test_end_of_term_case_once_under_concurrency(dorg: DOrg) -> None:
    p = await active(dorg, end=date.today() - timedelta(days=1))

    async def beat() -> None:
        async with tenant_session(dorg.tenant_id) as db:
            await end_alerts.run_end_of_term(_ctx(dorg), db)

    await asyncio.gather(*(beat() for _ in range(3)), return_exceptions=True)
    await beat()
    rows = await sql(
        dorg,
        "SELECT reason, initiated_by_kind FROM offboarding_cases WHERE placement_id = :i",
        i=uuid.UUID(p["id"]),
    )
    assert rows == [{"reason": "end_of_term", "initiated_by_kind": "system"}]


async def test_ending_soon_list(dorg: DOrg) -> None:
    p = await active(dorg, end=date.today() + timedelta(days=20))
    resp = await dorg.admin.client.get(f"{PL}/ending-soon", params={"within_days": 30})
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["placement_id"] == p["id"])
    assert row["days_left"] == 20
