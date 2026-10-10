"""Epic 44 story 44.1 (FR-222): plans, lines, validation, import, copy, scope / isolation."""

import io
import time

from fastapi import FastAPI

from tests.modules.commercials.helpers import COrg
from tests.modules.identity.conftest import csrf, join
from tests.modules.workforce.helpers import WF, act, add_line, err, line_body, ok, plan, skill


async def test_plan_crud_and_fiscal_label(corg: COrg) -> None:
    p = await plan(corg, client_id=str(corg.client_id))
    assert p["status"] == "draft" and p["origin"] == "internal"
    assert p["fiscal_label"] == "FY 27-28" and p["currency"] == "INR" and p["editable"]
    assert p["client_name"]
    listed = await ok(await corg.c.get(f"{WF}/plans?client_id={corg.client_id}", headers=corg.h()))
    assert [x["id"] for x in listed] == [p["id"]]
    upd = await ok(await corg.c.patch(f"{WF}/plans/{p['id']}", json={"name": "Renamed"}, headers=corg.h()))
    assert upd["name"] == "Renamed"
    # > 24 months
    resp = await corg.c.post(
        f"{WF}/plans",
        json={"name": "X", "period_start": "2027-04-01", "period_end": "2029-04-01"},
        headers=corg.h(),
    )
    err(resp, 422, "period_too_long")
    assert (await corg.c.delete(f"{WF}/plans/{p['id']}", headers=corg.h())).status_code == 204
    err(await corg.c.get(f"{WF}/plans/{p['id']}", headers=corg.h()), 404, "not_found")


async def test_line_validation(corg: COrg) -> None:
    java = await skill(corg, "Java")
    p = await plan(corg)
    line = await add_line(corg, p["id"], skills=[{"skill_key": java, "min_proficiency": 4}, {"name": "java"}])
    assert line["skills"] == [
        {"skill_key": java, "name": "Java", "min_proficiency": 4}
    ]  # name resolves, de-duplicated
    assert line["start_month"] == "2027-05" and line["period_end"] == "2028-03-31"
    assert line["budget_amount"] == "2400000.00" and line["status"] == "forecast"
    url = f"{WF}/plans/{p['id']}/lines"
    err(
        await corg.c.post(url, json=line_body(skills=[{"name": "Klingon Poetry"}]), headers=corg.h()),
        422,
        "line_invalid",
    )
    resp = await corg.c.post(
        url, json=line_body(skills=[{"skill_key": java, "min_proficiency": 6}]), headers=corg.h()
    )
    assert resp.status_code == 422
    resp = await corg.c.post(url, json=line_body(budget_amount=1200.5), headers=corg.h())
    assert resp.status_code == 422 and "floating-point" in resp.text, resp.text
    err(await corg.c.post(url, json=line_body(start_month="2026-01"), headers=corg.h()), 422, "line_invalid")
    err(await corg.c.post(url, json=line_body(headcount=501), headers=corg.h()), 422, "line_invalid")
    upd = await ok(
        await corg.c.patch(
            f"{url}/{line['id']}", json={"headcount": 5, "budget_amount": "100"}, headers=corg.h()
        )
    )
    assert upd["headcount"] == 5 and upd["budget_amount"] == "100.00"
    assert (await corg.c.delete(f"{url}/{line['id']}", headers=corg.h())).status_code == 204
    assert await ok(await corg.c.get(url, headers=corg.h())) == []


def _csv(rows: list[str]) -> bytes:
    return (
        "role,skills,headcount,type,location,start_month,budget,bill_rate,unit,notes\n" + "\n".join(rows)
    ).encode()


async def test_import_200_rows_with_7_bad(corg: COrg) -> None:
    await skill(corg, "Java")
    await skill(corg, "Spring")
    p = await plan(corg)
    rows = [
        f"Dev {i},Java:4; Spring:3,{1 + i % 3},new,Pune,2027-{4 + i % 9:02d},1000.00,,annual,"
        for i in range(193)
    ]
    rows += [
        ",Java,1,new,,2027-05,10,,,",  # no role
        "Dev,Cobol Wizardry,1,new,,2027-05,10,,,",  # unknown skill
        "Dev,Java:6,1,new,,2027-05,10,,,",  # proficiency 6
        "Dev,Java,x,new,,2027-05,10,,,",  # headcount
        "Dev,Java,1,new,,2025-05,10,,,",  # outside period
        "Dev,Java,1,new,,2027-05,10.555,,,",  # 3 dp money
        "Dev,Java,1,weird,,2027-05,10,,,",  # type
    ]
    files = {"file": ("plan.csv", _csv(rows), "text/csv")}
    started = time.monotonic()
    preview = await ok(await corg.c.post(f"{WF}/plans/{p['id']}/lines/import", files=files, headers=corg.h()))
    assert preview["row_count"] == 200 and preview["error_count"] == 7 and preview["valid_count"] == 193
    assert {e["row"] for e in preview["errors"]} == set(range(195, 202))
    assert not preview["applied"] and preview["created"] == 0
    applied = await ok(
        await corg.c.post(f"{WF}/plans/{p['id']}/lines/import?apply=true", files=files, headers=corg.h())
    )
    assert applied["created"] == 193
    assert time.monotonic() - started < 15
    lines = await ok(await corg.c.get(f"{WF}/plans/{p['id']}/lines", headers=corg.h()))
    assert len(lines) == 193 and all(ln["skills"][0]["min_proficiency"] == 4 for ln in lines)
    # the same file twice -> 409
    err(
        await corg.c.post(f"{WF}/plans/{p['id']}/lines/import?apply=true", files=files, headers=corg.h()),
        409,
        "import_already_applied",
    )


async def test_import_xlsx(corg: COrg) -> None:
    from openpyxl import Workbook

    await skill(corg, "Java")
    p = await plan(corg)
    wb = Workbook()
    ws = wb.active
    ws.append(
        [
            "Role",
            "Skills",
            "Headcount",
            "Type",
            "Location",
            "Start month",
            "Budget",
            "Bill rate",
            "Unit",
            "Notes",
        ]
    )
    ws.append(["QA Lead", "Java:3", 2, "replacement", "Chennai", "2027-06", "500000.00", None, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    files = {
        "file": (
            "plan.xlsx",
            buf.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    }
    out = await ok(
        await corg.c.post(f"{WF}/plans/{p['id']}/lines/import?apply=true", files=files, headers=corg.h())
    )
    assert out["created"] == 1, out
    (ln,) = await ok(await corg.c.get(f"{WF}/plans/{p['id']}/lines", headers=corg.h()))
    assert ln["line_type"] == "replacement" and ln["headcount"] == 2 and ln["budget_amount"] == "500000.00"


async def test_copy_shifts_months(corg: COrg) -> None:
    p = await plan(corg)
    await add_line(corg, p["id"], start_month="2027-05")
    await add_line(corg, p["id"], role_title="QA", start_month="2028-02", budget_amount="10.00")
    copy = await ok(
        await corg.c.post(
            f"{WF}/plans/{p['id']}/copy",
            json={"name": "FY 28-29", "period_start": "2028-04-01"},
            headers=corg.h(),
        ),
        201,
    )
    assert copy["period_end"] == "2029-03-31" and copy["status"] == "draft" and copy["slots"]["open"] == 0
    lines = await ok(await corg.c.get(f"{WF}/plans/{copy['id']}/lines", headers=corg.h()))
    assert sorted(ln["start_month"] for ln in lines) == ["2028-05", "2029-02"]
    assert {ln["budget_amount"] for ln in lines} == {"2400000.00", "10.00"}


async def test_transitions_and_budget_rules(corg: COrg) -> None:
    p = await plan(corg)
    err(await corg.c.post(f"{WF}/plans/{p['id']}/budget", headers=corg.h()), 422, "plan_empty")
    await add_line(corg, p["id"], budget_amount=None)
    e = err(await corg.c.post(f"{WF}/plans/{p['id']}/budget", headers=corg.h()), 422, "budget_missing")
    assert len(e["details"]["lines"]) == 1
    err(await corg.c.post(f"{WF}/plans/{p['id']}/submit", headers=corg.h()), 409, "invalid_transition")
    err(
        await corg.c.post(f"{WF}/plans/{p['id']}/start-revision", headers=corg.h()), 409, "invalid_transition"
    )
    err(await corg.c.post(f"{WF}/plans/{p['id']}/reopen", headers=corg.h()), 409, "invalid_transition")
    (ln,) = await ok(await corg.c.get(f"{WF}/plans/{p['id']}/lines", headers=corg.h()))
    await ok(
        await corg.c.patch(
            f"{WF}/plans/{p['id']}/lines/{ln['id']}", json={"budget_amount": "5000"}, headers=corg.h()
        )
    )
    budgeted = await act(corg, p["id"], "budget")
    assert budgeted["status"] == "budgeted"
    back = await act(corg, p["id"], "unbudget")
    assert back["status"] == "draft"


async def test_scope_and_isolation(app: FastAPI, corg: COrg, other_corg: COrg) -> None:
    p = await plan(corg)
    # another tenant -> 404 everywhere
    for method, url in (
        ("get", f"{WF}/plans/{p['id']}"),
        ("get", f"{WF}/plans/{p['id']}/lines"),
        ("get", f"{WF}/plans/{p['id']}/slots"),
        ("get", f"{WF}/plans/{p['id']}/actuals"),
        ("get", f"{WF}/plans/{p['id']}/revisions"),
        ("post", f"{WF}/plans/{p['id']}/budget"),
    ):
        resp = await getattr(other_corg.c, method)(url, headers=other_corg.h())
        assert resp.status_code == 404, (url, resp.text)
    assert await ok(await other_corg.c.get(f"{WF}/plans", headers=other_corg.h())) == []
    # own scope (Account Manager) does not see the admin's plan; a user without the key gets 403
    am = await join(app, corg.tenant, "account_manager", name="Ann Account")
    recruiter = await join(app, corg.tenant, "recruiter", name="Rey Recruiter")
    try:
        assert (await am.client.get(f"{WF}/plans/{p['id']}", headers=csrf(am.client))).status_code == 404
        mine = await ok(
            await am.client.post(
                f"{WF}/plans",
                json={"name": "AM plan", "period_start": "2027-04-01", "period_end": "2028-03-31"},
                headers=csrf(am.client),
            ),
            201,
        )
        assert [x["id"] for x in await ok(await am.client.get(f"{WF}/plans", headers=csrf(am.client)))] == [
            mine["id"]
        ]
        assert (await recruiter.client.get(f"{WF}/plans", headers=csrf(recruiter.client))).status_code == 403
    finally:
        await am.client.aclose()
        await recruiter.client.aclose()
