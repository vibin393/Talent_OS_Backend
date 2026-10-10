# ruff: noqa: E501
"""Epic 41 story 41.1 (FR-209): the internal job board -- projection, eligibility (tenure / grade / location / BU),
Worker board through ``/portal/work/opportunities``, Worker-safe payloads, org RLS on ``internal_openings``, the
careers re-verification and the rules / settings / grades endpoints."""

import uuid
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from app.core.database import OrgScope, tenant_session
from app.schemas.mobility_portal import WorkerOpeningDetailOut, WorkerOpeningOut
from tests.modules.demands.helpers import DEMANDS, DOrg, h
from tests.modules.mobility.helpers import WORK, M, active_worker, demand, employee, execute, worker
from tests.p2 import sign_in_external
from tests.portal.conftest import Site, careers, publish

FORBIDDEN = ("client", "rate", "salary", "compensation", "hiring_manager", "margin", "budget")


def _keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        out = set(obj)
        for v in obj.values():
            out |= _keys(v)
        return out
    if isinstance(obj, list):
        return set().union(*(_keys(v) for v in obj)) if obj else set()
    return set()


def test_worker_schemas_carry_no_internal_fields() -> None:
    for model in (WorkerOpeningOut, WorkerOpeningDetailOut):
        for name in model.model_fields:
            assert not any(f in name for f in FORBIDDEN), f"{model.__name__}.{name}"


async def test_board_lists_internal_and_both_not_external(app: FastAPI, dorg: DOrg) -> None:
    internal = await demand(dorg, "internal", title="Internal Data Engineer")
    both = await demand(dorg, "both", title="Both Data Engineer")
    await demand(dorg, "external", title="External Data Engineer")
    emp = await employee(app, dorg)
    r = await emp.client.get(f"{M}/openings")
    assert r.status_code == 200, r.text
    ids = {i["demand_id"] for i in r.json()["items"]}
    assert ids == {internal["id"], both["id"]}
    assert r.json()["board_enabled"] is True
    detail = await emp.client.get(f"{M}/openings/{internal['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["can_apply"] is True and body["disclosure_text"].startswith("Your current manager")
    assert body["skills"] and body["skills"][0]["name"] == "PySpark"
    # The external demand has no detail; turning it internal puts it on the board on the next read.
    ext = await demand(dorg, "external", title="Later Internal")
    assert (await emp.client.get(f"{M}/openings/{ext['id']}")).status_code == 404
    patched = await dorg.admin.client.patch(f"{DEMANDS}/{ext['id']}", json={"visibility": "internal"}, headers=h(dorg.admin))
    assert patched.status_code == 200, patched.text
    assert (await emp.client.get(f"{M}/openings/{ext['id']}")).status_code == 200
    # Closing a demand closes the opening.
    await execute(dorg, "UPDATE demands SET status = 'on_hold' WHERE id = :i", i=uuid.UUID(both["id"]))
    ids = {i["demand_id"] for i in (await emp.client.get(f"{M}/openings")).json()["items"]}
    assert both["id"] not in ids


async def test_eligibility_by_tenure_grade_location_and_bu(app: FastAPI, dorg: DOrg) -> None:
    d = await demand(dorg, "internal", title="Rule-bound role")
    rules = await dorg.admin.client.put(
        f"{M}/demands/{d['id']}/rules",
        json={"min_tenure_months": 6, "grades": ["L3", "L4"], "locations": ["Chennai"]},
        headers=h(dorg.admin),
    )
    assert rules.status_code == 200, rules.text
    assert rules.json()["on_board"] is True and rules.json()["visibility"] == "internal"

    # Worker with 3 months tenure, no grade, in Pune: not eligible with three reasons.
    _, cid = await active_worker(dorg, months=3, location="Pune")
    w = await worker(app, dorg, cid)
    assert (await w.get(WORK)).json()["items"] == []
    page = (await w.get(f"{WORK}?include_ineligible=true")).json()
    (item,) = page["items"]
    assert item["eligible"] is False
    reasons = " ".join(item["reasons"])
    assert "at least 6 months" in reasons and "no grade" in reasons and "Chennai" in reasons

    # Grade + location fixed via profiles, tenure still short -> one reason.
    r = await dorg.admin.client.put(
        f"{M}/profiles", json={"subject_kind": "worker", "subject_id": str(cid), "grade": "L3"}, headers=h(dorg.admin)
    )
    assert r.status_code == 200, r.text
    await execute(dorg, "UPDATE placements SET work_location_text = 'Chennai, TN' WHERE candidate_id = :c", c=cid)
    (item,) = (await w.get(f"{WORK}?include_ineligible=true")).json()["items"]
    assert len(item["reasons"]) == 1 and "6 months" in item["reasons"][0]
    applied = await w.post(f"{WORK}/{d['id']}/apply", json={"disclosure_ack": True}, headers={"X-CSRF-Token": "csrf-token"})
    assert applied.status_code == 409 and applied.json()["error"]["code"] == "not_eligible", applied.text

    # Long-tenured worker in Chennai with grade L4: eligible.
    _, cid2 = await active_worker(dorg, months=12, location="Chennai")
    await dorg.admin.client.put(f"{M}/profiles", json={"subject_kind": "worker", "subject_id": str(cid2), "grade": "l4"}, headers=h(dorg.admin))
    w2 = await worker(app, dorg, cid2)
    (item,) = (await w2.get(WORK)).json()["items"]
    assert item["eligible"] is True and item["reasons"] == []

    # Business units apply to Users only.
    bu = uuid.uuid4()
    await execute(dorg, "INSERT INTO business_units (id, tenant_id, name) VALUES (:i, :t, 'Delivery')", i=bu, t=dorg.tenant_id)
    d2 = await demand(dorg, "both", title="BU role")
    await dorg.admin.client.put(f"{M}/demands/{d2['id']}/rules", json={"business_unit_ids": [str(bu)]}, headers=h(dorg.admin))
    emp = await employee(app, dorg)
    items = {i["demand_id"]: i for i in (await emp.client.get(f"{M}/openings?include_ineligible=true")).json()["items"]}
    assert items[d2["id"]]["eligible"] is False and "business units" in items[d2["id"]]["reasons"][0]
    await execute(dorg, "UPDATE users SET business_unit_id = :b WHERE id = :u", b=bu, u=emp.user_id)
    items = {i["demand_id"]: i for i in (await emp.client.get(f"{M}/openings")).json()["items"]}
    assert d2["id"] in items


async def test_worker_board_payload_is_worker_safe(app: FastAPI, site: Site) -> None:
    from tests.modules.legal_entities.conftest import provision

    await provision(site.org.tenant)
    p = await publish(site, title="Internal Platform Engineer", visibility="internal")
    _, cid = await active_worker(site.org)
    w = await worker(app, site.org, cid)
    page = await w.get(WORK)
    assert page.status_code == 200, page.text
    detail = await w.get(f"{WORK}/{p['demand_id']}")
    assert detail.status_code == 200, detail.text
    for payload in (page.json(), detail.json()):
        raw = str(payload)
        assert "SENTINEL_CLIENT" not in raw and "9999" not in raw
        assert not any(any(f in k for f in FORBIDDEN) for k in _keys(payload))
    html = detail.json()["summary_html"]
    assert "<script" not in html and "data pipelines" in html and len(html) <= 4096


async def test_internal_openings_org_rls(app: FastAPI, dorg: DOrg) -> None:
    """Client / vendor sessions read zero ``internal_openings`` rows; worker and internal sessions read them."""
    d = await demand(dorg, "internal")
    emp = await employee(app, dorg)
    assert (await emp.client.get(f"{M}/openings")).status_code == 200  # refresh builds the projection
    for kind in ("client", "vendor"):
        async with tenant_session(dorg.tenant_id, org=OrgScope.of(kind, uuid.uuid4())) as db:
            assert (await db.execute(text("SELECT count(*) FROM internal_openings"))).scalar() == 0
    async with tenant_session(dorg.tenant_id, org=OrgScope.of("worker", uuid.uuid4())) as db:
        assert (await db.execute(text("SELECT count(*) FROM internal_openings WHERE demand_id = :d"), {"d": uuid.UUID(d["id"])})).scalar() == 1
        assert (await db.execute(text("SELECT count(*) FROM internal_opening_rules"))).scalar() == 0
        assert (await db.execute(text("SELECT count(*) FROM mobility_settings"))).scalar() == 0


async def test_external_cookies_and_cross_tenant_are_refused(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    from tests.p2 import make_client_user, make_vendor, make_vendor_user

    d = await demand(dorg, "internal")
    cu = await make_client_user(dorg.tenant_id, dorg.client_id)
    client_user = await sign_in_external(app, "client_user", cu, dorg.tenant_id, dorg.client_id)
    vid = await make_vendor(dorg.tenant_id)
    vu = await make_vendor_user(dorg.tenant_id, vid)
    vendor_user = await sign_in_external(app, "vendor_user", vu, dorg.tenant_id, vid)
    for ext in (client_user, vendor_user):
        for url in (f"{M}/openings", f"{M}/my-applications", WORK, f"{WORK}/applications"):
            assert (await ext.client.get(url)).status_code == 401, url
    other = await employee(app, other_dorg)
    assert (await other.client.get(f"{M}/openings/{d['id']}")).status_code == 404
    assert d["id"] not in {i["demand_id"] for i in (await other.client.get(f"{M}/openings")).json()["items"]}


async def test_careers_hide_internal_only_openings(site: Site, public: httpx.AsyncClient, app: FastAPI) -> None:
    """Re-verification of the foundation filter: internal-only is absent from careers but on the internal board."""
    internal = await publish(site, title="Internal Only Role", visibility="internal")
    both = await publish(site, title="Both Role", visibility="both")
    base = careers(site.slug)
    listed = {i["id"] for i in (await public.get(f"{base}/postings")).json()["items"]}
    assert both["posting_id"] in listed and internal["posting_id"] not in listed
    assert internal["posting_id"] not in {e["posting_id"] for e in (await public.get(f"{base}/sitemap")).json()}
    assert (await public.get(f"{base}/postings/{internal['posting_id']}")).status_code == 404
    emp = await employee(app, site.org)
    board = {i["demand_id"] for i in (await emp.client.get(f"{M}/openings")).json()["items"]}
    assert {internal["demand_id"], both["demand_id"]} <= board


async def test_settings_disable_boards_and_need_manage(app: FastAPI, dorg: DOrg) -> None:
    await demand(dorg, "internal")
    emp = await employee(app, dorg)
    assert (await emp.client.get(f"{M}/settings")).status_code == 403
    assert (await emp.client.put(f"{M}/profiles", json={"subject_kind": "user", "subject_id": str(emp.user_id), "grade": "L1"}, headers=emp.h())).status_code == 403
    got = await dorg.admin.client.get(f"{M}/settings")
    assert got.status_code == 200 and got.json()["board_enabled"] is True
    bad = await dorg.admin.client.patch(f"{M}/settings", json={"fallback_approver_role_key": "nope"}, headers=h(dorg.admin))
    assert bad.status_code == 422
    r = await dorg.admin.client.patch(f"{M}/settings", json={"board_enabled": False, "worker_board_enabled": False}, headers=h(dorg.admin))
    assert r.status_code == 200, r.text
    page = (await emp.client.get(f"{M}/openings")).json()
    assert page["board_enabled"] is False and page["items"] == []
    _, cid = await active_worker(dorg)
    w = await worker(app, dorg, cid)
    assert (await w.get(WORK)).json()["board_enabled"] is False


async def test_profiles_csv_import_preview_then_apply(app: FastAPI, dorg: DOrg) -> None:
    emp = await employee(app, dorg)
    csv_text = f"email,grade,location\n{emp.email},L5,Bengaluru\nnobody@example.com,L1,\n"
    files = {"file": ("grades.csv", csv_text.encode(), "text/csv")}
    preview = await dorg.admin.client.post(f"{M}/profiles/import?validate_only=true", files=files, headers=h(dorg.admin))
    assert preview.status_code == 200, preview.text
    assert preview.json()["errors"] == 1 and preview.json()["applied"] == 0
    good = f"email,grade,location\n{emp.email},L5,Bengaluru\n"
    done = await dorg.admin.client.post(
        f"{M}/profiles/import?validate_only=false", files={"file": ("g.csv", good.encode(), "text/csv")}, headers=h(dorg.admin)
    )
    assert done.status_code == 200 and done.json()["applied"] == 1, done.text
    (p,) = (await dorg.admin.client.get(f"{M}/profiles?subject_kind=user")).json()
    assert (p["grade"], p["location"], p["subject_email"]) == ("L5", "Bengaluru", emp.email)
