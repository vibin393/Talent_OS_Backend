# ruff: noqa: E501
"""47.3: viewer-scoped runs, masking, sharing rules, pins, cross-tenant isolation."""

import uuid

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.identity import Role
from tests.reporting.builder.helpers import BASE, SRC, create_report, example_definition, seed_placements, share_tenant
from tests.reporting.helpers import API, ROrg, h


async def test_shared_report_runs_as_the_viewer_with_masking(rorg: ROrg) -> None:
    seeded = await seed_placements(rorg)
    admin, rec = rorg.admin, rorg.recruiter
    # Aggregate report (bill sum + ratio) and a row report (bill_rate column), both shared tenant-wide.
    agg = await create_report(admin)
    rows_def = {"source": SRC, "fields": ["human_id", "client.name", "bill_rate"], "sort": [{"key": "human_id"}]}
    listing = await create_report(admin, rows_def, {"type": "table"}, name="Placement rates")
    for r in (agg, listing):
        await share_tenant(admin, r["id"])

    # Owner run: everything, nothing masked.
    full = (await admin.client.post(f"{BASE}/{agg['id']}/run", headers=h(admin))).json()
    assert full["masked_fields"] == [] and sum(x["placements"] for x in full["rows"]) == 4

    # Recruiter (placement:read team -> own rows p1, p3; no compensation:view) sees the shared reports...
    resp = await rec.client.get(BASE, params={"tab": "shared"}, headers=h(rec))
    assert {x["id"] for x in resp.json()["items"]} == {agg["id"], listing["id"]}
    resp = await rec.client.post(f"{BASE}/{agg['id']}/run", headers=h(rec))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ... only their in-scope placements, with the bill / pay measures and the ratio removed and listed as masked.
    assert sum(x["placements"] for x in body["rows"]) == 2
    assert {c["key"] for c in body["columns"]} == {"client.name", "planned_start_date", "placements"}
    assert set(body["masked_fields"]) == {"bill", "pay", "bill_pay", "bill_rate", "pay_rate"}
    assert body["viz"]["value"] == "placements"  # the chart falls back to a remaining measure
    resp = await rec.client.post(f"{BASE}/{listing['id']}/run", headers=h(rec))
    body = resp.json()
    assert {r["human_id"] for r in body["rows"]} == set(await _human_ids(rorg, seeded.p["p1"], seeded.p["p3"]))
    assert all(r["bill_rate"] is None for r in body["rows"]) and body["masked_fields"] == ["bill_rate"]

    # A report that groups / filters by a compensation field fails for that viewer (never silently dropped).
    grouped = await create_report(admin, {"source": SRC, "filters": {"field": "bill_rate", "op": "gt", "value": 10}, "group_by": [{"field": "client.name"}]}, {"type": "bar", "x": "client.name"}, name="Filtered")
    await share_tenant(admin, grouped["id"])
    resp = await rec.client.post(f"{BASE}/{grouped['id']}/run", headers=h(rec))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "semantic_field_forbidden"

    # Recruiter B is a different scope: p2, p4.
    rb = rorg.recruiter_b
    body = (await rb.client.post(f"{BASE}/{listing['id']}/run", headers=h(rb))).json()
    assert {r["human_id"] for r in body["rows"]} == set(await _human_ids(rorg, seeded.p["p2"], seeded.p["p4"]))


async def _human_ids(org: ROrg, *ids: uuid.UUID) -> list[str]:
    from app.models.placements import Placement

    async with tenant_session(org.tenant_id) as db:
        return list((await db.execute(select(Placement.human_id).where(Placement.id.in_(ids)))).scalars())


async def test_private_report_is_invisible_and_cross_tenant_404(rorg: ROrg, other_rorg: ROrg) -> None:
    await seed_placements(rorg)
    report = await create_report(rorg.account)
    # Not shared: another user of the tenant gets 404 (existence not revealed).
    resp = await rorg.manager.client.post(f"{BASE}/{report['id']}/run", headers=h(rorg.manager))
    assert resp.status_code == 404
    resp = await rorg.manager.client.get(f"{BASE}/{report['id']}", headers=h(rorg.manager))
    assert resp.status_code == 404
    # Another tenant's admin: 404 on read, run and download, and its own preview sees none of tenant A's rows.
    oa = other_rorg.admin
    for method, path in (("get", f"{BASE}/{report['id']}"), ("post", f"{BASE}/{report['id']}/run")):
        resp = await getattr(oa.client, method)(path, headers=h(oa))
        assert resp.status_code == 404
    resp = await oa.client.post(f"{BASE}/preview", json={"definition": example_definition(), "viz": {"type": "table"}}, headers=h(oa))
    assert resp.status_code == 200 and resp.json()["rows"] == []


async def test_sharing_rules(rorg: ROrg) -> None:
    await seed_placements(rorg)
    am, admin = rorg.account, rorg.admin
    report = await create_report(am)
    # Tenant / role shares need report:share_tenant (the Account Manager lacks it) -> 403.
    resp = await am.client.put(f"{BASE}/{report['id']}/shares", json=[{"target_kind": "tenant"}], headers=h(am))
    assert resp.status_code == 403
    async with tenant_session(rorg.tenant_id) as db:
        roles = {r.key: r.id for r in (await db.execute(select(Role))).scalars()}
    resp = await am.client.put(f"{BASE}/{report['id']}/shares", json=[{"target_kind": "role", "role_id": str(roles["recruiter"])}], headers=h(am))
    assert resp.status_code == 403
    # The admin's own report: a Role without placement:read (Interviewer) is refused with 422.
    own = await create_report(admin)
    resp = await admin.client.put(f"{BASE}/{own['id']}/shares", json=[{"target_kind": "role", "role_id": str(roles["interviewer"])}], headers=h(admin))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "share_role_lacks_permission"
    # A Role that can read placements is accepted; its members see the report in "Shared with me".
    resp = await admin.client.put(f"{BASE}/{own['id']}/shares", json=[{"target_kind": "role", "role_id": str(roles["recruiter"])}], headers=h(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["target_kind"] == "role"
    listed = (await rorg.recruiter.client.get(BASE, params={"tab": "shared"}, headers=h(rorg.recruiter))).json()["items"]
    assert [x["id"] for x in listed] == [own["id"]] and listed[0]["visibility"] == "shared"
    assert (await rorg.manager.client.get(BASE, params={"tab": "shared"}, headers=h(rorg.manager))).json()["items"] == []
    # Clearing the shares makes it private again.
    resp = await admin.client.put(f"{BASE}/{own['id']}/shares", json=[], headers=h(admin))
    assert resp.status_code == 200 and resp.json() == []
    assert (await rorg.recruiter.client.get(f"{BASE}/{own['id']}", headers=h(rorg.recruiter))).status_code == 404


async def test_pins_need_visibility_and_keep_order(rorg: ROrg) -> None:
    await seed_placements(rorg)
    admin, rec = rorg.admin, rorg.recruiter
    a = await create_report(admin, name="A")
    b = await create_report(admin, name="B")
    # Not visible to the recruiter yet -> 404.
    assert (await rec.client.post(f"{BASE}/{a['id']}/pin", headers=h(rec))).status_code == 404
    await share_tenant(admin, a["id"])
    await share_tenant(admin, b["id"])
    for r in (a, b):
        resp = await rec.client.post(f"{BASE}/{r['id']}/pin", headers=h(rec))
        assert resp.status_code == 200, resp.text
    pins = (await rec.client.get(f"{API}/me/report-pins", headers=h(rec))).json()
    assert [p["report_id"] for p in pins] == [a["id"], b["id"]]
    pins = (await rec.client.put(f"{API}/me/report-pins/order", json={"report_ids": [b["id"], a["id"]]}, headers=h(rec))).json()
    assert [p["report_id"] for p in pins] == [b["id"], a["id"]]
    assert pins[0]["viz"]["type"] == "bar"
    # A pinned render is a normal viewer-scoped run (cached); unsharing hides the pin.
    resp = await rec.client.post(f"{BASE}/{a['id']}/run", params={"cached": "true"}, headers=h(rec))
    assert resp.status_code == 200 and "bill" not in {c["key"] for c in resp.json()["columns"]}
    await admin.client.put(f"{BASE}/{b['id']}/shares", json=[], headers=h(admin))
    pins = (await rec.client.get(f"{API}/me/report-pins", headers=h(rec))).json()
    assert [p["report_id"] for p in pins] == [a["id"]]
    resp = await rec.client.delete(f"{BASE}/{a['id']}/pin", headers=h(rec))
    assert resp.status_code == 200 and resp.json() == []
