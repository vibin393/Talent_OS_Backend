"""Epic 38 stories 38.4 / 38.5: templates (FR-193), rediscovery with Pools, pins, add-to-pipeline (FR-194/195)."""

import uuid
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.permissions import Scope
from app.core.semantic import SemanticContext, compile, validate
from app.models.pipeline import Application
from app.services.talent_pools import rediscovery, templates
from tests.epic5 import h
from tests.modules.demands.helpers import DOrg, open_demand
from tests.modules.identity.conftest import join
from tests.modules.talent_pools.helpers import (
    POOLS,
    active_member_ids,
    add_match,
    candidate,
    create_pool,
    ctx_for,
    reached_stage,
)

API = "/api/v1"


async def test_template_definitions_compile_against_base_sources(dorg: DOrg) -> None:
    async with tenant_session(dorg.tenant_id) as db:
        sctx = SemanticContext.for_system(dorg.tenant_id, db, "refresh")
        for key, t in templates.TEMPLATES.items():
            if key == "campus":
                continue
            defn = validate({"source": t.source_key, "filters": t.filters})
            assert (await compile(defn, sctx)).ids_stmt is not None, key


async def test_template_listing_enable_and_campus_available(dorg: DOrg) -> None:
    raw = await dorg.admin.client.get(f"{POOLS}/templates")
    assert raw.status_code == 200, raw.text
    listed = {t["key"]: t for t in (await dorg.admin.client.get(f"{POOLS}/templates")).json()}
    assert set(listed) == set(templates.TEMPLATES)
    assert all(not t["enabled"] for t in listed.values())  # disabled by default
    assert listed["bench"]["available"] is True
    # Epic 43 registered ``campus_batch``, so the campus template is now available.
    assert listed["campus"]["available"] is True and listed["campus"]["unavailable_reason"] is None
    assert (await dorg.admin.client.get(POOLS)).json() == []  # nothing created automatically

    resp = await dorg.admin.client.post(f"{POOLS}/templates/immediate_joiners/enable", headers=h(dorg.admin))
    assert resp.status_code == 201, resp.text
    pool = resp.json()
    assert (pool["kind"], pool["visibility"], pool["template_key"]) == ("dynamic", "tenant", "immediate_joiners")
    again = await dorg.admin.client.post(f"{POOLS}/templates/immediate_joiners/enable", headers=h(dorg.admin))
    assert again.status_code == 409 and again.json()["error"]["code"] == "template_enabled"
    listed = {t["key"]: t for t in (await dorg.admin.client.get(f"{POOLS}/templates")).json()}
    assert listed["immediate_joiners"]["enabled"] is True and listed["immediate_joiners"]["pool_id"] == pool["id"]

    campus = await dorg.admin.client.post(f"{POOLS}/templates/campus_2027/enable", headers=h(dorg.admin))
    assert campus.status_code == 201, campus.text
    assert campus.json()["template_key"] == "campus_2027"
    listed = {t["key"]: t for t in (await dorg.admin.client.get(f"{POOLS}/templates")).json()}
    assert listed["campus"]["enabled"] is True


async def _application(org: DOrg, demand_id: str, candidate_id: uuid.UUID) -> uuid.UUID:
    resp = await org.admin.client.post(
        f"{API}/demands/{demand_id}/applications", json={"candidate_ids": [str(candidate_id)]}, headers=h(org.admin)
    )
    assert resp.status_code in (200, 201), resp.text
    return uuid.UUID(resp.json()["items"][0]["application_id"])


async def test_silver_medalists_17_months_in_19_months_out(dorg: DOrg) -> None:
    demand = await open_demand(dorg.admin, dorg.client_id)
    recent = await candidate(dorg.admin, "Offered 17 months ago")
    old = await candidate(dorg.admin, "Offered 19 months ago")
    await reached_stage(dorg.tenant_id, await _application(dorg, demand["id"], recent), "offered", 517)
    await reached_stage(dorg.tenant_id, await _application(dorg, demand["id"], old), "offered", 578)
    resp = await dorg.admin.client.post(f"{POOLS}/templates/silver_medalists/enable", headers=h(dorg.admin))
    assert resp.status_code == 201, resp.text
    assert await active_member_ids(dorg.tenant_id, resp.json()["id"]) == {recent}


async def test_rediscovery_pools_section_pins_and_add_to_pipeline(app: FastAPI, dorg: DOrg) -> None:
    demand = await open_demand(dorg.admin, dorg.client_id)
    did = demand["id"]
    ids = [await candidate(dorg.admin, f"Cand {i}") for i in range(3)]
    for cid, score in zip(ids, ("91.0", "72.5", "80.0"), strict=True):
        await add_match(dorg.tenant_id, did, cid, score)
    hot = await create_pool(dorg.admin, name="Silver Medalists")
    await dorg.admin.client.post(
        f"{POOLS}/{hot['id']}/members", json={"candidate_ids": [str(c) for c in ids]}, headers=h(dorg.admin)
    )
    empty = await create_pool(dorg.admin, name="Empty but pinned")

    pin = await dorg.admin.client.post(f"{API}/demands/{did}/pool-pins", json={"pool_id": empty["id"]}, headers=h(dorg.admin))
    assert pin.status_code == 201, pin.text
    pins = (await dorg.admin.client.get(f"{API}/demands/{did}/pool-pins")).json()
    assert [p["pool_id"] for p in pins] == [empty["id"]]

    summary = await dorg.admin.client.get(f"{API}/demands/{did}/rediscovery")
    assert summary.status_code == 200, summary.text
    section = summary.json()["sections"]["pools"]
    assert section["matching_pools"] == 1 and section["summary"] == "1 matching Pool"
    names = [p["name"] for p in section["pools"]]
    assert names == ["Empty but pinned", "Silver Medalists"]  # pinned first, even at zero
    top = section["pools"][1]["top"]
    assert [t["candidate_id"] for t in top] == [str(ids[0]), str(ids[2]), str(ids[1])]
    assert [t["rank"] for t in top] == [1, 2, 3] and top[0]["overall"] == "91.0"
    assert "funnel" in summary.json()

    added = await dorg.admin.client.post(
        f"{API}/demands/{did}/pools/{hot['id']}/add-to-pipeline", json={"top_n": 2}, headers=h(dorg.admin)
    )
    assert added.status_code == 200, added.text
    assert added.json()["counts"] == {"created": 2}
    async with tenant_session(dorg.tenant_id) as db:
        apps = (await db.execute(select(Application).where(Application.demand_id == uuid.UUID(did)))).scalars().all()
    assert {(a.source, a.source_ref) for a in apps} == {("saved_list", f"pool:{hot['id']}")}
    assert {a.candidate_id for a in apps} == {ids[0], ids[2]}

    unpin = await dorg.admin.client.delete(f"{API}/demands/{did}/pool-pins/{empty['id']}", headers=h(dorg.admin))
    assert unpin.status_code == 204

    # an out-of-scope member is not listed for an own-scope viewer
    rec = await join(app, dorg.tenant, "recruiter", name="Rita")
    try:
        own = ctx_for(dorg.tenant_id, rec.user_id, candidate_scope=Scope.OWN)
        async with tenant_session(dorg.tenant_id) as db:
            out: dict[str, Any] | None = await rediscovery.pools_section(own, db, uuid.UUID(did))
        assert out is not None and out["matching_pools"] == 0
    finally:
        await rec.client.aclose()
