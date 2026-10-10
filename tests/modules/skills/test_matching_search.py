# ruff: noqa: E501
"""Story 37.4 (FR-190): graph-aware matching (expansion credit, P1 parent floor, neutral proficiency, expansion off,
determinism, graph change -> stale) and graph-aware search (``expand_skills``)."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.matching import Match
from app.services.matching import ports
from tests.epic5 import h, search_ok
from tests.modules.demands.helpers import DOrg, open_demand
from tests.modules.skills.helpers import add_edge, candidate_with, gkey, skill_id


@pytest.fixture(autouse=True)
def _unavailable_pipeline() -> Iterator[None]:
    previous = ports.pipeline_gateway()
    ports.register_pipeline_gateway(None)
    yield
    ports.register_pipeline_gateway(previous)


async def _matches(m: Any, demand_id: str) -> dict[str, dict[str, Any]]:
    resp = await m.client.get(f"/api/v1/demands/{demand_id}/matches")
    assert resp.status_code == 200, resp.text
    return {i["candidate"]["display_name"]: i for i in resp.json()["items"]}


async def _rows(tid: Any, demand_id: str) -> dict[str, Any]:
    async with tenant_session(tid) as db:
        return {
            str(r.candidate_id): r
            for r in (
                await db.execute(select(Match).where(Match.demand_id == uuid.UUID(demand_id)))
            ).scalars()
        }


async def test_graph_aware_matching(dorg: DOrg) -> None:
    m, tid = dorg.admin, dorg.tenant_id
    edge = await add_edge(
        m, await gkey("Spring Boot", tid), await gkey("Spring MVC", tid), "related_to", "0.7"
    )
    assert edge.status_code == 201, edge.text
    await candidate_with(m, "Mia Mvc", ["Spring MVC"], current_title="Developer")
    await candidate_with(m, "Sid Spring", ["Spring"], current_title="Developer")
    await candidate_with(m, "Nia NoProf", ["Spring Boot"], current_title="Developer")
    demand = await open_demand(
        m,
        dorg.client_id,
        title="Spring dev",
        skills=[{"label": "Spring Boot", "kind": "mandatory", "min_proficiency": 3}],
        mandatory_skills=[],
    )
    rows = await _matches(m, demand["id"])
    mia = rows["Mia Mvc"]
    assert mia["sub_scores"]["skills"] == 70
    assert any("(related to Spring Boot, 0.7)" in e["text"] for e in mia["evidence"])
    sid = rows.get("Sid Spring")  # P1 parent floor 0.4 is credited but stays below the mandatory threshold
    assert sid is None or sid["missing_mandatory"] == ["Spring Boot"]
    nia = rows["Nia NoProf"]
    assert nia["sub_scores"]["skills"] == 100
    assert any("proficiency not recorded" in e["text"] for e in nia["evidence"])
    before = await _rows(tid, demand["id"])
    # re-run: identical scores and hashes
    recompute = await m.client.post(f"/api/v1/demands/{demand['id']}/matches/recompute", headers=h(m))
    assert recompute.status_code in (200, 202), recompute.text
    after = await _rows(tid, demand["id"])
    assert {k: (v.overall, v.input_hash) for k, v in before.items()} == {
        k: (v.overall, v.input_hash) for k, v in after.items()
    }
    assert all(r.scoring_version == "2" for r in after.values())
    # a graph change marks the demand's matches stale
    await m.client.delete(f"/api/v1/skill-graph/edges/{edge.json()['id']}", headers=h(m))
    assert all(r.is_stale for r in (await _rows(tid, demand["id"])).values())


async def test_expansion_off_on_demand_skill(dorg: DOrg) -> None:
    m, tid = dorg.admin, dorg.tenant_id
    await add_edge(m, await gkey("Spring Boot", tid), await gkey("Spring MVC", tid), "related_to", "0.7")
    await candidate_with(m, "Mo Mvc", ["Spring MVC"], current_title="Developer")
    demand = await open_demand(
        m,
        dorg.client_id,
        title="Exact only",
        skills=[{"label": "Spring Boot", "kind": "mandatory", "expand": False}],
        mandatory_skills=[],
    )
    got = (await m.client.get(f"/api/v1/demands/{demand['id']}")).json()["skills"][0]
    assert got["expand"] is False
    rows = await _matches(m, demand["id"])
    assert "Mo Mvc" not in rows or rows["Mo Mvc"]["sub_scores"]["skills"] == 0


async def test_search_expansion_toggle(dorg: DOrg) -> None:
    m, tid = dorg.admin, dorg.tenant_id
    await add_edge(m, await gkey("Fortran", tid), await gkey("MATLAB", tid), "implies", "0.8")
    await candidate_with(m, "Fred Fortran", ["Fortran"])
    await candidate_with(m, "Mat Matlab", ["MATLAB"])
    matlab = str(await skill_id("MATLAB", tid))
    on = await search_ok(m, filters={"skills": [matlab]})
    assert {i["display_name"] for i in on["items"]} == {"Fred Fortran", "Mat Matlab"}
    off = await search_ok(m, filters={"skills": [matlab], "expand_skills": False})
    assert {i["display_name"] for i in off["items"]} == {"Mat Matlab"}
