# ruff: noqa: E501
"""47.2: definitions, versions, preview / run, the 47.2 example numbers, FY buckets, extra='forbid', viz keys."""

from decimal import Decimal

from sqlalchemy import func, select

from app.core.database import tenant_session
from app.models.report_builder import ReportDefinitionVersion, ReportRun
from tests.reporting.builder.helpers import BASE, SRC, create_report, example_definition, seed_placements
from tests.reporting.helpers import ROrg, h


def _by(rows: list[dict], *keys: str) -> dict[tuple, dict]:
    return {tuple(r[k] for k in keys): r for r in rows}


async def test_example_report_numbers_preview_run_and_versions(rorg: ROrg) -> None:
    seeded = await seed_placements(rorg)
    am = rorg.account  # Account Manager: build + compensation; placement:read team -> p1, p2 (AM rows)
    admin = rorg.admin
    # Preview as the Tenant Admin (all four placements).
    resp = await admin.client.post(f"{BASE}/preview", json={"definition": example_definition(), "viz": {"type": "bar", "x": "planned_start_date", "series": "client.name", "value": "bill"}}, headers=h(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = _by(body["rows"], "client.name", "planned_start_date")
    acme, globex = seeded.acme_name, seeded.globex_name
    assert set(rows) == {(acme, "2026-03-01"), (acme, "2026-04-01"), (globex, "2026-04-01"), (globex, "2026-05-01")}
    r = rows[(acme, "2026-04-01")]
    assert (r["placements"], Decimal(r["bill"]), Decimal(r["pay"])) == (1, Decimal("200"), Decimal("150"))
    assert Decimal(r["bill_pay"]).quantize(Decimal("0.0001")) == Decimal("1.3333")
    assert Decimal(rows[(globex, "2026-04-01")]["bill_pay"]) == Decimal("1.5")
    assert body["masked_fields"] == [] and body["limit_hit"] is None and body["row_limit"] == 500
    assert [c["key"] for c in body["columns"]] == ["client.name", "planned_start_date", "placements", "bill", "pay", "bill_pay"]

    # Saved by the Account Manager; their own run is scoped to their placements (p1, p2 -> Acme only).
    report = await create_report(am)
    assert report["definition_version"] == 1 and report["is_owner"] and report["status"] == "ok"
    resp = await am.client.post(f"{BASE}/{report['id']}/run", headers=h(am))
    assert resp.status_code == 200, resp.text
    run = resp.json()
    assert {(x["client.name"], x["planned_start_date"]) for x in run["rows"]} == {(acme, "2026-03-01"), (acme, "2026-04-01")}
    assert run["definition_version"] == 1 and run["run_id"]

    # Editing the definition appends a version; renaming alone does not.
    resp = await am.client.patch(f"{BASE}/{report['id']}", json={"name": "Renamed"}, headers=h(am))
    assert resp.status_code == 200 and resp.json()["definition_version"] == 1
    new_def = example_definition(group_by=[{"field": "client.name"}, {"field": "planned_start_date", "bucket": "fy"}])
    resp = await am.client.patch(f"{BASE}/{report['id']}", json={"definition": new_def}, headers=h(am))
    assert resp.status_code == 200, resp.text
    assert resp.json()["definition_version"] == 2
    async with tenant_session(rorg.tenant_id) as db:
        versions = (await db.execute(select(func.count()).select_from(ReportDefinitionVersion).where(ReportDefinitionVersion.report_id == report["id"]))).scalar_one()
        runs = (await db.execute(select(ReportRun.run_kind, ReportRun.definition_version).where(ReportRun.principal_user_id.in_([am.user_id, admin.user_id])))).all()
    assert versions == 2
    assert ("preview", None) in runs and ("run", 1) in runs

    # Listing: Mine tab for the owner; nothing shared yet for the admin's "shared" tab.
    resp = await am.client.get(BASE, params={"tab": "mine"}, headers=h(am))
    assert [x["id"] for x in resp.json()["items"]] == [report["id"]]
    resp = await admin.client.get(BASE, params={"tab": "shared"}, headers=h(admin))
    assert resp.json()["items"] == []


async def test_fy_bucket_across_march_april(rorg: ROrg) -> None:
    await seed_placements(rorg)
    admin = rorg.admin
    defn = {"source": SRC, "group_by": [{"field": "planned_start_date", "bucket": "fy"}], "measures": [{"key": "n", "agg": "count"}]}
    resp = await admin.client.post(f"{BASE}/preview", json={"definition": defn, "viz": {"type": "bar", "x": "planned_start_date", "value": "n"}}, headers=h(admin))
    assert resp.status_code == 200, resp.text
    # 2026-03-31 belongs to FY 25-26; 2026-04-01 onwards to FY 26-27 (April start, core.fiscal).
    assert {r["planned_start_date"]: r["n"] for r in resp.json()["rows"]} == {"25-26": 1, "26-27": 3}
    # "FY month" = month bucket with an fy filter on the same date field (between the FY bounds).
    defn = {
        "source": SRC,
        "filters": {"field": "planned_start_date", "op": "between", "value": ["2026-04-01", "2027-03-31"]},
        "group_by": [{"field": "planned_start_date", "bucket": "month"}],
        "measures": [{"key": "n", "agg": "count"}],
    }
    resp = await admin.client.post(f"{BASE}/preview", json={"definition": defn, "viz": {"type": "line", "x": "planned_start_date", "value": "n"}}, headers=h(admin))
    assert {r["planned_start_date"]: r["n"] for r in resp.json()["rows"]} == {"2026-04-01": 2, "2026-05-01": 1}


async def test_no_expression_anywhere_and_validation_codes(rorg: ROrg) -> None:
    admin = rorg.admin
    good_viz = {"type": "table"}
    # An ``expression`` key in the definition, the viz or the request body -> 422 (extra forbidden).
    resp = await admin.client.post(f"{BASE}/preview", json={"definition": {**example_definition(), "expression": "bill_rate * 2"}, "viz": good_viz}, headers=h(admin))
    assert resp.status_code == 422 and resp.json()["error"]["code"].startswith("semantic_")
    resp = await admin.client.post(f"{BASE}/preview", json={"definition": example_definition(measures=[{"key": "x", "agg": "sum", "field": "bill_rate", "expression": "1"}]), "viz": good_viz}, headers=h(admin))
    assert resp.status_code == 422
    resp = await admin.client.post(f"{BASE}/preview", json={"definition": example_definition(), "viz": {"type": "bar", "expression": "x"}}, headers=h(admin))
    assert resp.status_code == 422
    resp = await admin.client.post(BASE, json={"name": "x", "definition": example_definition(), "expression": "1"}, headers=h(admin))
    assert resp.status_code == 422
    # Unknown field / operator -> the foundation's codes with details.path.
    resp = await admin.client.post(f"{BASE}/preview", json={"definition": {"source": SRC, "fields": ["salary"]}, "viz": good_viz}, headers=h(admin))
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "semantic_unknown_field" and err["details"]["path"] == "fields[0]"
    resp = await admin.client.post(f"{BASE}/preview", json={"definition": {"source": SRC, "filters": {"field": "status", "op": "regex", "value": "x"}, "fields": ["id"]}, "viz": good_viz}, headers=h(admin))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "semantic_operator_not_allowed"
    # Viz keys must belong to the definition.
    resp = await admin.client.post(f"{BASE}/preview", json={"definition": example_definition(), "viz": {"type": "bar", "x": "status", "value": "bill"}}, headers=h(admin))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "report_viz_invalid"
    # Injection attempt is just a bound value.
    resp = await admin.client.post(f"{BASE}/preview", json={"definition": {"source": SRC, "filters": {"field": "role_title", "op": "contains", "value": "x'; DROP TABLE placements;--"}, "fields": ["id"]}, "viz": good_viz}, headers=h(admin))
    assert resp.status_code == 200 and resp.json()["rows"] == []


async def test_builder_permissions(rorg: ROrg) -> None:
    # A Recruiter has no report:build: preview and create are refused.
    rec = rorg.recruiter
    resp = await rec.client.post(f"{BASE}/preview", json={"definition": example_definition(), "viz": {"type": "table"}}, headers=h(rec))
    assert resp.status_code == 403
    resp = await rec.client.post(BASE, json={"name": "x", "definition": example_definition()}, headers=h(rec))
    assert resp.status_code == 403
