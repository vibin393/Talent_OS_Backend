"""Report list views (A4): shape, columns, filters, sorting, paging and compensation masking."""

import uuid

from sqlalchemy import insert

from app.core.database import tenant_session
from app.models.identity import Role
from tests.modules.identity.conftest import Member, join
from tests.reporting.helpers import API, ROrg
from tests.reporting.timeline import RANGE, build_march

LISTS = f"{API}/reports/lists"


async def reporter(app: object, org: ROrg, *, compensation: bool = False) -> Member:
    """Create a custom role: report read/export + every list's read permission, compensation only if asked."""
    perms = dict.fromkeys(
        (
            "report:read",
            "report:export",
            "candidate:read",
            "demand:read",
            "application:read",
            "submission:read",
            "interview:read",
            "offer:read",
        ),
        "tenant",
    )
    if compensation:
        perms["compensation:view"] = "tenant"
    key = f"reporter_{uuid.uuid4().hex[:6]}"
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            insert(Role).values(
                id=uuid.uuid4(),
                tenant_id=org.tenant_id,
                key=key,
                name=key,
                is_system=False,
                permissions=perms,
            )
        )
    return await join(app, org.tenant, key, name="Remi Reporter")  # type: ignore[arg-type]


async def test_list_shape_and_columns(rorg: ROrg) -> None:
    await build_march(rorg)
    resp = await rorg.admin.client.get(f"{LISTS}/submissions", params={**RANGE, "page_size": 2})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {"items", "total", "page", "page_size", "columns"} <= set(body)
    assert body["total"] == 5 and len(body["items"]) == 2 and body["page_size"] == 2
    cols = {c["key"]: c for c in body["columns"]}
    assert cols["bill_rate"]["compensation"] is True and cols["bill_rate"]["masked"] is False
    item = body["items"][0]
    assert item["bill_rate"] == {"amount": "1500.00", "currency": "INR"}
    # Default sort: newest sent first.
    assert item["sent_at"] >= body["items"][1]["sent_at"]
    page2 = (
        await rorg.admin.client.get(f"{LISTS}/submissions", params={**RANGE, "page_size": 2, "page": 3})
    ).json()
    assert len(page2["items"]) == 1


async def test_compensation_masked_without_permission(app: object, rorg: ROrg) -> None:
    await build_march(rorg)
    remi = await reporter(app, rorg)
    try:
        body = (await remi.client.get(f"{LISTS}/submissions", params=RANGE)).json()
        cols = {c["key"]: c for c in body["columns"]}
        for key in ("pay_rate", "bill_rate", "margin_amount", "margin_pct"):
            assert cols[key]["masked"] is True
            assert all(i[key] is None for i in body["items"])
        offers = (await remi.client.get(f"{LISTS}/offers")).json()
        assert all(i["compensation"] is None for i in offers["items"])
    finally:
        await remi.client.aclose()


async def test_application_filters(rorg: ROrg) -> None:
    march = await build_march(rorg)
    get = rorg.admin.client.get

    async def total(**params: str) -> int:
        resp = await get(f"{LISTS}/applications", params=params)
        assert resp.status_code == 200, resp.text
        return int(resp.json()["total"])

    assert await total(**RANGE) == 6  # a1-a5 and a7 (a6 was created in February)
    assert await total(**RANGE, reached="hired") == 2
    assert await total(stage="hired") == 3
    assert await total(stage="applied,shortlisted") == 1  # a5 (a7 is submitted)
    assert await total(source="career_portal") == 2
    assert await total(demand_id=str(march.demands["D1"])) == 2
    assert await total(client_id=str(march.c2)) == 2  # a6 (D5), a7 (D4)
    assert await total(active="true") == 4  # a3, a4, a5, a7
    assert (await get(f"{LISTS}/applications", params={"sort": "nope"})).status_code == 422


async def test_submission_filters(rorg: ROrg) -> None:
    await build_march(rorg)

    async def total(**params: str) -> int:
        resp = await rorg.admin.client.get(f"{LISTS}/submissions", params=params)
        assert resp.status_code == 200, resp.text
        return int(resp.json()["total"])

    assert await total(sent_by=str(rorg.recruiter_b.user_id)) == 1
    assert await total(status="draft") == 1
    assert await total(feedback="none", older_than="3") == 4
    assert await total(**RANGE, db_sourced="true") == 3  # s1, s3 and recruiter B's s4
    assert await total(**RANGE, has_interview="true") == 1
    assert await total(business_unit_ids=str(rorg.bu_b)) == 1


async def test_interview_and_offer_filters(rorg: ROrg) -> None:
    await build_march(rorg)
    iv = (await rorg.admin.client.get(f"{LISTS}/interviews", params=RANGE)).json()
    assert iv["total"] == 1  # the cancelled one is excluded by default
    cancelled = (await rorg.admin.client.get(f"{LISTS}/interviews", params={"status": "cancelled"})).json()
    assert cancelled["total"] == 1
    offers = (await rorg.admin.client.get(f"{LISTS}/offers", params={"status": "released,viewed"})).json()
    assert offers["total"] == 2
    ranged = (
        await rorg.admin.client.get(f"{LISTS}/offers", params={"from": "2026-03-20", "to": "2026-03-20"})
    ).json()
    assert ranged["total"] == 1


async def test_demands_list_dashboard_filters(rorg: ROrg) -> None:
    march = await build_march(rorg)

    async def ids(**params: str) -> set[str]:
        resp = await rorg.admin.client.get(f"{API}/demands", params={"page_size": 100, **params})
        assert resp.status_code == 200, resp.text
        return {i["id"] for i in resp.json()["items"]}

    d = {k: str(v) for k, v in march.demands.items()}
    assert await ids(sla="breached") == {d["D3"]}
    assert await ids(opened_from="2026-03-02", opened_to="2026-03-04") == {d["D1"], d["D4"], d["D6"]}
    assert await ids(business_unit_ids=str(rorg.bu_b)) == {d["D4"]}
    recruiter = await rorg.recruiter.client.get(f"{API}/demands", params={"assignee": "me", "page_size": 100})
    assert {i["id"] for i in recruiter.json()["items"]} == {d["D1"], d["D2"], d["D3"]}
