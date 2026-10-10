# ruff: noqa: E501
"""Story 37.1 (FR-187): platform seed in the DB, tenant edges (CRUD, weight / cycle / duplicate / approval rules,
concurrency), expansion through the API with a fresh cache, settings, permissions and cross-tenant isolation."""

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text

from app.core.audit import AuditLog
from app.core.database import owner_engine, tenant_session
from app.core.errors import ConflictError
from app.models.candidates import Skill, TenantSkill
from app.models.skills_graph import SkillEdge
from app.schemas.skills import SkillEdgeIn
from app.services import skills_service
from app.services.skills import graph as graph_mod
from tests.epic5 import h, make_candidate
from tests.modules.demands.helpers import DOrg, admin_ctx
from tests.modules.identity.conftest import join
from tests.modules.skills.helpers import GRAPH, add_edge, gkey


async def _preview(m: Any, refs: list[str], depth: int = 1) -> dict[str, dict[str, Any]]:
    resp = await m.client.post(f"{GRAPH}/expand-preview", json={"refs": refs, "depth": depth}, headers=h(m))
    assert resp.status_code == 200, resp.text
    return {i["key"]: i for i in resp.json()["items"]}


async def test_platform_seed_loaded(dorg: DOrg) -> None:
    async with tenant_session(dorg.tenant_id) as db:
        active = (
            await db.execute(select(func.count()).select_from(Skill).where(Skill.is_active))
        ).scalar_one()
        edges = (await db.execute(select(func.count()).select_from(SkillEdge))).scalar_one()
    assert active >= 1500 and edges > 1000
    # no platform parent_of cycle
    engine = owner_engine()
    try:
        async with engine.connect() as conn:
            cyc = (
                await conn.execute(
                    text(
                        "WITH RECURSIVE w(start, node, depth) AS (SELECT from_skill_id, to_skill_id, 1 FROM skill_edges "
                        "WHERE kind='parent_of' UNION ALL SELECT w.start, e.to_skill_id, w.depth + 1 FROM w JOIN "
                        "skill_edges e ON e.from_skill_id = w.node AND e.kind='parent_of' WHERE w.depth < 25) "
                        "SELECT count(*) FROM w WHERE start = node"
                    )
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert cyc == 0
    java = await gkey("Java", dorg.tenant_id)
    rows = await _preview(dorg.admin, [java])
    assert (
        rows[await gkey("Spring Boot", dorg.tenant_id)]["weight"] == "0.80"
    )  # seed: Spring Boot implies Java
    sb_rows = await _preview(dorg.admin, [await gkey("Spring Boot", dorg.tenant_id)])
    assert java not in sb_rows


async def test_tenant_edge_lifecycle_audit_and_fresh_cache(dorg: DOrg) -> None:
    m = dorg.admin
    fortran, matlab = await gkey("Fortran", dorg.tenant_id), await gkey("MATLAB", dorg.tenant_id)
    assert fortran not in await _preview(m, [matlab])
    v1 = (await m.client.get(f"{GRAPH}/settings")).json()["graph_version"]
    resp = await add_edge(m, fortran, matlab, "implies", "0.8")
    assert resp.status_code == 201, resp.text
    edge = resp.json()
    assert edge["origin"] == "tenant" and edge["weight"] == "0.80" and edge["to_skill"]["name"] == "MATLAB"
    assert (await m.client.get(f"{GRAPH}/settings")).json()["graph_version"] != v1
    rows = await _preview(m, [matlab])
    assert rows[fortran]["weight"] == "0.80" and rows[fortran]["path"][0]["kind"] == "implies"
    assert matlab not in await _preview(m, [fortran])  # implies is one way
    # node view lists it as outgoing for Fortran
    node = (await m.client.get(f"{GRAPH}/skills/{fortran}")).json()
    assert any(e["id"] == edge["id"] for e in node["outgoing"])
    assert (await m.client.get(f"{GRAPH}/skills/not-a-key")).status_code == 422
    assert (await m.client.get(f"{GRAPH}/skills/global:{uuid.uuid4()}")).status_code == 404
    # update + delete
    upd = await m.client.patch(f"{GRAPH}/edges/{edge['id']}", json={"weight": "0.5"}, headers=h(m))
    assert upd.status_code == 200 and upd.json()["weight"] == "0.50"
    assert (await _preview(m, [matlab]))[fortran]["weight"] == "0.50"
    assert (await m.client.delete(f"{GRAPH}/edges/{edge['id']}", headers=h(m))).status_code == 204
    assert fortran not in await _preview(m, [matlab])
    async with tenant_session(dorg.tenant_id) as db:
        actions = set(
            (await db.execute(select(AuditLog.action).where(AuditLog.action.like("skill_graph.%")))).scalars()
        )
    assert {"skill_graph.edge_created", "skill_graph.edge_updated", "skill_graph.edge_deleted"} <= actions


@pytest.mark.parametrize("weight", ["1.2", 0, "abc", None])
async def test_weight_invalid(dorg: DOrg, weight: Any) -> None:
    resp = await add_edge(
        dorg.admin,
        await gkey("Fortran", dorg.tenant_id),
        await gkey("MATLAB", dorg.tenant_id),
        "related_to",
        weight,
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "skill_edge_weight_invalid"


async def test_cycle_duplicate_self_and_pending(dorg: DOrg) -> None:
    m, tid = dorg.admin, dorg.tenant_id
    java, spring_boot = await gkey("Java", tid), await gkey("Spring Boot", tid)
    resp = await add_edge(m, spring_boot, java, "parent_of")  # Java -> Spring -> Spring Boot already
    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert body["code"] == "skill_graph_cycle" and body["details"]["path"][0] == spring_boot
    assert java in body["details"]["path"]
    dup = await add_edge(m, spring_boot, java, "implies")  # platform edge
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "skill_edge_exists"
    assert (await add_edge(m, java, java, "related_to")).json()["error"]["code"] == "skill_edge_self"
    await make_candidate(m, "Pending Skill Person", skills=["FooLangXYZ"])
    async with tenant_session(tid) as db:
        pending = (
            (await db.execute(select(TenantSkill).where(TenantSkill.status == "pending"))).scalars().first()
        )
    assert pending is not None
    bad = await add_edge(m, f"tenant:{pending.id}", java, "implies")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "skill_not_approved"


async def test_tenant_parent_cycle_with_tenant_edges_and_concurrency(dorg: DOrg) -> None:
    tid = dorg.tenant_id
    a, b = await gkey("COBOL", tid), await gkey("Fortran", tid)
    ctx = admin_ctx(dorg)

    async def create(src: str, dst: str) -> str:
        try:
            async with tenant_session(tid) as db:
                await skills_service.create_edge(
                    ctx, db, SkillEdgeIn(from_key=src, to_key=dst, kind="parent_of", weight="0.8")
                )
            return "ok"
        except ConflictError as exc:
            return exc.code

    results = sorted(await asyncio.gather(create(a, b), create(b, a)))
    assert results == ["ok", "skill_graph_cycle"]


async def test_permissions_and_cross_tenant(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    fortran, matlab = await gkey("Fortran", dorg.tenant_id), await gkey("MATLAB", dorg.tenant_id)
    recruiter = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
    try:
        assert (await add_edge(recruiter, fortran, matlab, "implies")).status_code == 403
        put = await recruiter.client.put(
            f"{GRAPH}/settings", json={"min_weight": "0.5"}, headers=h(recruiter)
        )
        assert put.status_code == 403
        assert (await recruiter.client.get(f"{GRAPH}/settings")).status_code == 200
    finally:
        await recruiter.client.aclose()
    edge = (await add_edge(dorg.admin, fortran, matlab, "implies")).json()
    other = other_dorg.admin
    assert (
        await other.client.patch(f"{GRAPH}/edges/{edge['id']}", json={"weight": "0.5"}, headers=h(other))
    ).status_code == 404
    assert (await other.client.delete(f"{GRAPH}/edges/{edge['id']}", headers=h(other))).status_code == 404
    assert edge["id"] not in {e["id"] for e in (await other.client.get(f"{GRAPH}/edges")).json()}
    assert fortran not in await _preview(other, [matlab])


async def test_settings_update_and_audit(dorg: DOrg) -> None:
    m = dorg.admin
    before = (await m.client.get(f"{GRAPH}/settings")).json()
    assert before["min_weight"] == "0.30" and before["match_depth"] == 2
    resp = await m.client.put(f"{GRAPH}/settings", json={"min_weight": "0.5", "match_depth": 1}, headers=h(m))
    assert resp.status_code == 200, resp.text
    assert resp.json()["min_weight"] == "0.50" and resp.json()["graph_version"] != before["graph_version"]
    bad = await m.client.put(f"{GRAPH}/settings", json={"min_weight": "2"}, headers=h(m))
    assert bad.status_code == 422
    assert (await m.client.put(f"{GRAPH}/settings", json={"match_depth": 3}, headers=h(m))).status_code == 422


async def test_depth_out_of_range(dorg: DOrg) -> None:
    resp = await dorg.admin.client.post(
        f"{GRAPH}/expand-preview",
        json={"refs": [await gkey("Java", dorg.tenant_id)], "depth": 3},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "depth_out_of_range"


async def test_semantic_expander_registered(dorg: DOrg) -> None:
    from app.core.semantic import operators
    from app.core.skill_refs import SkillKey

    java = SkillKey.parse(await gkey("Java", dorg.tenant_id))
    async with tenant_session(dorg.tenant_id) as db:
        rows = await operators.expand_skills(
            admin_ctx(dorg), db, operators.SkillMatch(ref=java, min_proficiency=None, expand=True)
        )
    assert len(rows) > 1 and rows[0].key == java
    assert graph_mod.cached_keys()
