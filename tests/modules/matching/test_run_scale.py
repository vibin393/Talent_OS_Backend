"""Story 5.5: rediscovery run over 5,000 candidates completes in < 10 s (fake embedder)."""

import uuid

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.matching import MatchRun
from tests.epic5_seed import purge, seed
from tests.modules.demands.helpers import DOrg, open_demand


async def test_5k_candidate_run_under_10_seconds(dorg: DOrg) -> None:
    await seed(dorg.tenant_id, dorg.admin.user_id, 5000)
    try:
        await _check(dorg)
    finally:
        await purge(dorg.tenant_id)


async def _check(dorg: DOrg) -> None:
    demand = await open_demand(
        dorg.admin,
        dorg.client_id,
        mandatory_skills=["Databricks", "PySpark"],
        location="Chennai",
        country="IN",
    )
    async with tenant_session(dorg.tenant_id) as db:
        run = (
            await db.execute(select(MatchRun).where(MatchRun.demand_id == uuid.UUID(demand["id"])))
        ).scalar_one()
    assert run.status == "completed"
    assert run.funnel["total"] == 5000
    assert run.funnel["relevant"] >= 1000
    elapsed = (run.finished_at - run.started_at).total_seconds()
    print(f"5k run: {elapsed:.2f}s, considered={run.considered}, funnel={run.funnel}")
    assert elapsed < 10
    resp = await dorg.admin.client.get(f"/api/v1/demands/{demand['id']}/matches", params={"page_size": 100})
    body = resp.json()
    assert resp.status_code == 200 and body["total"] == 500
    overall = [i["overall"] for i in body["items"]]
    assert overall == sorted(overall, reverse=True)
