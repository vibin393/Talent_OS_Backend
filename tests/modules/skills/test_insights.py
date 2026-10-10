"""Story 37.5 (FR-191): skills supply and demand insight -- counts, proficiency buckets, deep-link params,
scope (own vs tenant), permission and a p95 performance check (``-m slow``)."""

import statistics
import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal, Scope
from app.models.demands import Demand
from app.services import skills_service
from tests.epic5 import h
from tests.modules.demands.helpers import DOrg, open_demand
from tests.modules.identity.conftest import join
from tests.modules.skills.helpers import CANDIDATES, candidate_skill_ids, candidate_with, gkey

INSIGHTS = "/api/v1/skills/insights"


async def _rows(m: Any, **params: Any) -> dict[str, dict[str, Any]]:
    resp = await m.client.get(INSIGHTS, params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["week_starts"]) == 13
    return {r["name"]: r for r in body["items"]}


async def test_counts_buckets_and_links(dorg: DOrg) -> None:
    m, tid = dorg.admin, dorg.tenant_id
    a = await candidate_with(m, "Ann Rust", ["Rust"])
    await candidate_with(m, "Ben Rust", ["Rust"])
    csid = (await candidate_skill_ids(tid, a["id"]))["Rust"]
    await m.client.post(
        f"{CANDIDATES}/{a['id']}/skills/{csid}/evidence",
        json={"kind": "recruiter", "proficiency": 4},
        headers=h(m),
    )
    await open_demand(m, dorg.client_id, title="Rust dev", mandatory_skills=["Rust"])
    rows = await _rows(m, skill=[await gkey("Rust", tid)])
    rust = rows["Rust"]
    assert rust["open_demand"] == 1 and rust["supply_total"] == 2 and rust["verified_supply"] == 1
    assert rust["supply_by_proficiency"]["4"] == 1 and rust["supply_by_proficiency"]["unknown"] == 1
    assert sum(rust["trend_90d"]["supply"]) == 2 and sum(rust["trend_90d"]["demand"]) == 1
    sid = (await gkey("Rust", tid)).split(":", 1)[1]
    assert rust["links"]["candidates"] == {"skills": [sid], "skills_mode": "all"}
    assert rust["bench"] == 0  # admin holds bench:read
    top = await _rows(m)
    assert "Rust" in top


async def test_scope_own_vs_tenant_and_permission(app: FastAPI, dorg: DOrg) -> None:
    m, tid = dorg.admin, dorg.tenant_id
    await candidate_with(m, "Admin Owned", ["Haskell"])
    rm = await join(app, dorg.tenant, "recruitment_manager", name="Rhea Manager")
    viewer = await join(app, dorg.tenant, "viewer", name="Vic Viewer")
    try:
        await candidate_with(rm, "Manager Owned", ["Haskell"])
        demand = await open_demand(m, dorg.client_id, title="Haskell dev", mandatory_skills=["Haskell"])
        async with tenant_session(tid) as db:  # owned by the manager
            row = await db.get(Demand, uuid.UUID(demand["id"]))
            assert row is not None
            row.owner_id = rm.user_id
        key = await gkey("Haskell", tid)
        assert (await _rows(m, skill=[key]))["Haskell"]["supply_total"] == 2
        assert (await viewer.client.get(INSIGHTS)).status_code == 403
        own = {"candidate:read": Scope.OWN, "demand:read": Scope.OWN, "skills_insight:read": Scope.OWN}
        for user_id, supply, demand in ((rm.user_id, 1, 1), (m.user_id, 1, 0)):
            ctx = RequestContext(
                tenant_id=tid, principal=Principal(type="user", id=user_id, tenant_id=tid, permissions=own)
            )
            async with tenant_session(tid) as db:
                out = await skills_service.skill_insights(ctx, db, skill_refs=[key])
            assert (out.items[0].supply_total, out.items[0].open_demand) == (supply, demand)
            assert out.items[0].bench is None  # no bench:read
    finally:
        await rm.client.aclose()
        await viewer.client.aclose()


async def test_cross_tenant_isolation(dorg: DOrg, other_dorg: DOrg) -> None:
    await candidate_with(dorg.admin, "Iso Late", ["Elixir"])
    key = await gkey("Elixir", dorg.tenant_id)
    assert (await _rows(other_dorg.admin, skill=[key]))["Elixir"]["supply_total"] == 0


@pytest.mark.slow
async def test_insights_p95_under_3s(dorg: DOrg) -> None:
    m = dorg.admin
    for i in range(40):
        await candidate_with(m, f"Perf Person {i}", ["Java", "Python", "SQL", "Docker"][: 1 + i % 4])
    durations = []
    for _ in range(20):
        start = time.perf_counter()
        resp = await m.client.get(INSIGHTS)
        durations.append(time.perf_counter() - start)
        assert resp.status_code == 200
    p95 = statistics.quantiles(durations, n=20)[-1]
    assert p95 < 3.0, p95
