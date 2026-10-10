"""Story 4.1: demand CRUD, REQ sequence and prefix, validation, filters, scope + assignee visibility,
compensation masking, client 360 section."""

import uuid

from fastapi import FastAPI
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import tenant_session
from tests.modules.demands.helpers import (
    CLIENTS,
    DEMANDS,
    DOrg,
    create_client,
    create_demand,
    h,
    open_demand,
    ready_body,
)
from tests.modules.identity.conftest import join


async def test_create_assigns_sequential_req_ids(dorg: DOrg) -> None:
    a = await create_demand(dorg.admin, dorg.client_id)
    b = await create_demand(dorg.admin, dorg.client_id, title="QA Lead")
    assert a["req_id"] == "REQ-0001"
    assert b["req_id"] == "REQ-0002"
    assert a["status"] == "draft"
    assert a["progress"] is None
    assert a["owner_id"] == str(dorg.admin.user_id)
    assert a["client_name"]
    assert a["margin_percent"] == "26.67"
    assert [s["label"] for s in a["skills"]] == ["PySpark"]


async def test_prefix_change_keeps_numbering(dorg: DOrg) -> None:
    await create_demand(dorg.admin, dorg.client_id)
    resp = await dorg.admin.client.put(
        "/api/v1/settings/demands", json={"req_prefix": "job"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["req_prefix"] == "JOB"
    b = await create_demand(dorg.admin, dorg.client_id)
    assert b["req_id"] == "JOB-0002"
    bad = await dorg.admin.client.put(
        "/api/v1/settings/demands", json={"req_prefix": "1x"}, headers=h(dorg.admin)
    )
    assert bad.status_code == 422


async def test_unknown_skill_is_pending(dorg: DOrg) -> None:
    d = await create_demand(
        dorg.admin, dorg.client_id, mandatory_skills=["Snowflakez"], preferred_skills=["SQL"]
    )
    skills = {s["label"]: s for s in d["skills"]}
    assert skills["Snowflakez"]["skill_status"] == "pending"
    assert skills["Snowflakez"]["kind"] == "mandatory"
    assert skills["Snowflakez"]["skill_id"] is not None
    assert any(s["kind"] == "preferred" for s in d["skills"])


async def test_validation_errors(dorg: DOrg) -> None:
    m = dorg.admin
    cases = [
        {"positions": 0},
        {"min_experience_years": "8", "max_experience_years": "3"},
        {"pay_rate_min": "10", "pay_rate_max": "5"},
        {"travel_percent": 101},
        {"country": "IND"},
        {"req_id": "REQ-9999"},
        {"status": "open"},
    ]
    for override in cases:
        resp = await m.client.post(DEMANDS, json=ready_body(dorg.client_id, **override), headers=h(m))
        assert resp.status_code == 422, (override, resp.text)


async def test_archived_client_and_foreign_contact(dorg: DOrg) -> None:
    m = dorg.admin
    archived = await create_client(m)
    await m.client.post(f"{CLIENTS}/{archived}/archive", headers=h(m))
    resp = await m.client.post(DEMANDS, json=ready_body(archived), headers=h(m))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "client_archived"

    other = await create_client(m)
    contact = await m.client.post(f"{CLIENTS}/{other}/contacts", json={"name": "Hema"}, headers=h(m))
    assert contact.status_code == 201, contact.text
    resp = await m.client.post(
        DEMANDS, json=ready_body(dorg.client_id, hiring_manager_contact_id=contact.json()["id"]), headers=h(m)
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_contact"
    missing = await m.client.post(DEMANDS, json=ready_body(uuid.uuid4()), headers=h(m))
    assert missing.status_code == 422


async def test_update_rules_and_read_only_fields(dorg: DOrg) -> None:
    m = dorg.admin
    d = await create_demand(m, dorg.client_id)
    url = f"{DEMANDS}/{d['id']}"
    resp = await m.client.patch(url, json={"title": "Lead Data Engineer", "positions": 3}, headers=h(m))
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Lead Data Engineer"
    for field, value in (
        ("req_id", "X-1"),
        ("status", "open"),
        ("progress", "offer"),
        ("positions_filled", 2),
    ):
        bad = await m.client.patch(url, json={field: value}, headers=h(m))
        assert bad.status_code == 422, field
    bad = await m.client.patch(
        url, json={"min_experience_years": "20", "max_experience_years": "5"}, headers=h(m)
    )
    assert bad.status_code == 422
    resp = await m.client.patch(
        url, json={"skills": [{"label": "Databricks", "kind": "mandatory"}]}, headers=h(m)
    )
    assert [s["label"] for s in resp.json()["skills"]] == ["Databricks"]


async def test_edits_are_audited(dorg: DOrg) -> None:
    d = await create_demand(dorg.admin, dorg.client_id)
    await dorg.admin.client.patch(f"{DEMANDS}/{d['id']}", json={"title": "Changed"}, headers=h(dorg.admin))
    async with tenant_session(dorg.tenant_id) as db:
        actions = (await db.execute(select(AuditLog.action).where(AuditLog.entity_id == d["id"]))).scalars()
        assert {"demand.created", "demand.updated"} <= set(actions)


async def test_filters_and_sort(dorg: DOrg) -> None:
    m = dorg.admin
    a = await create_demand(m, dorg.client_id, title="Alpha Engineer", priority="low")
    b = await open_demand(m, dorg.client_id, title="Beta Analyst", priority="critical")
    other_client = await create_client(m)
    await create_demand(m, other_client, title="Gamma Tester", priority="medium")

    async def ids(**params: object) -> list[str]:
        resp = await m.client.get(DEMANDS, params=params)
        assert resp.status_code == 200, resp.text
        return [i["id"] for i in resp.json()["items"]]

    assert await ids(status=["open"]) == [b["id"]]
    assert set(await ids(client_id=str(dorg.client_id))) == {a["id"], b["id"]}
    assert await ids(priority=["low"]) == [a["id"]]
    assert await ids(q=a["req_id"]) == [a["id"]]
    assert await ids(q="beta") == [b["id"]]
    assert (await ids(sort="-priority"))[0] == b["id"]
    assert (await ids(sort="priority"))[0] == a["id"]
    assert len(await ids(owner_id=str(m.user_id))) == 3
    bad = await m.client.get(DEMANDS, params={"assigned_to": "someone"})
    assert bad.status_code == 422


async def test_archive(dorg: DOrg) -> None:
    m = dorg.admin
    d = await create_demand(m, dorg.client_id)
    opened = await open_demand(m, dorg.client_id)
    resp = await m.client.post(f"{DEMANDS}/{opened['id']}/archive", headers=h(m))
    assert resp.status_code == 409
    resp = await m.client.post(f"{DEMANDS}/{d['id']}/archive", headers=h(m))
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is not None
    listed = [i["id"] for i in (await m.client.get(DEMANDS)).json()["items"]]
    assert d["id"] not in listed
    listed = [
        i["id"] for i in (await m.client.get(DEMANDS, params={"include_archived": True})).json()["items"]
    ]
    assert d["id"] in listed


async def test_scope_own_and_assignee_visibility(app: FastAPI, dorg: DOrg) -> None:
    am1 = await join(app, dorg.tenant, "account_manager", name="Amy Account")
    am2 = await join(app, dorg.tenant, "account_manager", name="Ben Account")
    recruiter = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
    try:
        mine = await create_demand(am1, dorg.client_id)
        assert (await am2.client.get(f"{DEMANDS}/{mine['id']}")).status_code == 404
        assert (await am1.client.get(f"{DEMANDS}/{mine['id']}")).status_code == 200
        ids2 = [i["id"] for i in (await am2.client.get(DEMANDS)).json()["items"]]
        assert mine["id"] not in ids2
        # PATCH by another own-scoped user: 404 (not visible)
        resp = await am2.client.patch(f"{DEMANDS}/{mine['id']}", json={"title": "x"}, headers=h(am2))
        assert resp.status_code == 404

        # Recruiter (team scope, no team): sees nothing until assigned.
        opened = await open_demand(dorg.admin, dorg.client_id)
        assert (await recruiter.client.get(f"{DEMANDS}/{opened['id']}")).status_code == 404
        resp = await dorg.admin.client.put(
            f"{DEMANDS}/{opened['id']}/assignments",
            json={"user_ids": [str(recruiter.user_id)], "primary_user_id": str(recruiter.user_id)},
            headers=h(dorg.admin),
        )
        assert resp.status_code == 200, resp.text
        assert (await recruiter.client.get(f"{DEMANDS}/{opened['id']}")).status_code == 200
        mine_list = (await recruiter.client.get(DEMANDS, params={"assigned_to": "me"})).json()["items"]
        assert [i["id"] for i in mine_list] == [opened["id"]]
        assert mine_list[0]["primary_recruiter"]["id"] == str(recruiter.user_id)
    finally:
        for m in (am1, am2, recruiter):
            await m.client.aclose()


async def test_compensation_masked_without_permission(app: FastAPI, dorg: DOrg) -> None:
    recruiter = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
    try:
        opened = await open_demand(dorg.admin, dorg.client_id)
        await dorg.admin.client.put(
            f"{DEMANDS}/{opened['id']}/assignments",
            json={"user_ids": [str(recruiter.user_id)]},
            headers=h(dorg.admin),
        )
        body = (await recruiter.client.get(f"{DEMANDS}/{opened['id']}")).json()
        assert body["bill_rate"] is None
        assert body["pay_rate_max"] is None
        assert body["margin_percent"] is None
        assert "bill_rate" in body["masked_fields"]
        assert body["currency"] == "INR"
        full = (await dorg.admin.client.get(f"{DEMANDS}/{opened['id']}")).json()
        assert full["bill_rate"] == "1500.00"
        assert full["masked_fields"] == []
    finally:
        await recruiter.client.aclose()


async def test_client_360_lists_demands(dorg: DOrg) -> None:
    d = await create_demand(dorg.admin, dorg.client_id)
    resp = await dorg.admin.client.get(f"{CLIENTS}/{dorg.client_id}/360")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [x["id"] for x in body["demands"]] == [d["id"]]
    assert body["counts"]["demands"] == 1


async def test_reason_codes_endpoint(dorg: DOrg) -> None:
    resp = await dorg.admin.client.get(f"{DEMANDS}/reason-codes")
    assert resp.status_code == 200
    assert "other" in resp.json()["closed"]
