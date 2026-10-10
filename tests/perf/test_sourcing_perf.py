# ruff: noqa: E501
"""Epic 56 story 56.5 performance (``-m slow``): 100,000 Candidates, 500 matches and 3 pinned Pools of 2,000 members;
a Fake-LLM sourcing run (read, rank, save, finish) completes in under 2 minutes."""

import time
import uuid

import pytest
from sqlalchemy import text

from app.core.database import tenant_session
from app.models.agent_runtime import AgentRun
from tests.agents.conftest import AgentOrg, _agent_env, _agents_registered, agent_org  # noqa: F401
from tests.agents.sourcing.helpers import DONE, GET, RANKED, SAVE, Seed, enable_sourcing, rows, run_agent
from tests.epic5_seed import seed as bulk_seed
from tests.modules.demands.helpers import create_client, open_demand

pytestmark = [pytest.mark.slow, pytest.mark.perf]


async def test_run_over_100k_candidates_under_two_minutes(agent_org: AgentOrg) -> None:  # noqa: F811
    tid, owner = agent_org.tenant_id, agent_org.admin.user_id
    client_id = await create_client(agent_org.admin)
    demand_id = uuid.UUID((await open_demand(agent_org.admin, client_id))["id"])
    await bulk_seed(tid, owner, 100_000, with_embeddings=False)
    async with tenant_session(tid) as db:
        await db.execute(
            text(
                "INSERT INTO matches (id, tenant_id, demand_id, candidate_id, overall, sub_scores, scoring_version, input_hash, computed_at) "
                "SELECT gen_random_uuid(), CAST(:t AS uuid), CAST(:d AS uuid), id, 50 + (row_number() OVER (ORDER BY id)) % 50, "
                "'{}'::jsonb, 'perf', repeat('x', 64), now() FROM candidates WHERE human_id LIKE 'SEED-%' ORDER BY id LIMIT 500"
            ),
            {"t": str(tid), "d": str(demand_id)},
        )
        for i in range(3):
            pool_id = uuid.uuid4()
            await db.execute(
                text(
                    "INSERT INTO talent_pools (id, tenant_id, name, kind, owner_id, visibility, status, member_count) "
                    "VALUES (:p, CAST(:t AS uuid), :n, 'static', CAST(:o AS uuid), 'tenant', 'active', 2000)"
                ),
                {"p": str(pool_id), "t": str(tid), "n": f"Perf pool {i}", "o": str(owner)},
            )
            await db.execute(
                text(
                    "INSERT INTO talent_pool_members (id, tenant_id, pool_id, candidate_id, source) "
                    "SELECT gen_random_uuid(), CAST(:t AS uuid), CAST(:p AS uuid), id, 'manual' FROM candidates "
                    "WHERE human_id LIKE 'SEED-%' ORDER BY human_id OFFSET :off LIMIT 2000"
                ),
                {"t": str(tid), "p": str(pool_id), "off": i * 2000},
            )
            await db.execute(
                text(
                    "INSERT INTO demand_pool_pins (id, tenant_id, demand_id, pool_id, pinned_by) "
                    "VALUES (gen_random_uuid(), CAST(:t AS uuid), CAST(:d AS uuid), CAST(:p AS uuid), CAST(:o AS uuid))"
                ),
                {"t": str(tid), "d": str(demand_id), "p": str(pool_id), "o": str(owner)},
            )
    await enable_sourcing(tid)
    seed = Seed(tenant_id=tid, admin=agent_org.admin, demand_id=demand_id, c={})
    d = str(demand_id)
    started = time.perf_counter()
    ref = await run_agent(seed, [
        {"kind": "tool_calls", "calls": [[GET, {"demand_id": d}], [RANKED, {"demand_id": d, "limit": 100}]]},
        {"kind": "tool_calls", "calls": [[SAVE, {"demand_id": d, "candidates": []}]]},
        DONE,
    ])  # fmt: skip
    elapsed = time.perf_counter() - started
    run = (await rows(tid, AgentRun, AgentRun.id == ref.run_id))[0]
    assert run.status == "completed", run.reason_code
    assert elapsed < 120, f"sourcing run took {elapsed:.1f}s"
