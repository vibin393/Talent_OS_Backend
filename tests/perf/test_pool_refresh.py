"""Epic 38 NFR: a dynamic Pool matching 100,000 Candidates refreshes in under 2 minutes (``-m slow``)."""

import time
import uuid

import pytest
from sqlalchemy import insert, select

from app.core.database import tenant_session
from app.models.candidates import Candidate
from app.models.talent_pools import TalentPool, TalentPoolRefresh
from app.services.talent_pools import refresh as pool_refresh
from tests.modules.demands.helpers import DOrg
from tests.modules.talent_pools.helpers import dynamic_pool

N = 100_000

pytestmark = [pytest.mark.slow, pytest.mark.perf]


async def test_refresh_100k_members_under_two_minutes(dorg: DOrg) -> None:
    pool = await dynamic_pool(dorg.admin, {"filters": {"field": "notice_period_days", "op": "lte", "value": 30}})
    async with tenant_session(dorg.tenant_id) as db:
        human = (await db.execute(select(Candidate.human_id).limit(1))).scalar_one_or_none()
        for start in range(0, N, 10_000):
            await db.execute(
                insert(Candidate),
                [
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": dorg.tenant_id,
                        "human_id": f"PERF-{i:07d}",
                        "full_name": f"Perf {i}",
                        "owner_id": dorg.admin.user_id,
                        "notice_period_days": 15,
                        "source": "manual",
                    }
                    for i in range(start, start + 10_000)
                ],
            )
    assert human is None or human
    async with tenant_session(dorg.tenant_id) as db:
        p = await db.get(TalentPool, uuid.UUID(pool["id"]))
        assert p is not None
        row = TalentPoolRefresh(tenant_id=dorg.tenant_id, pool_id=p.id, trigger="manual", status="running")
        db.add(row)
        await db.flush()
        rid = row.id
    started = time.monotonic()
    async with tenant_session(dorg.tenant_id) as db:
        out = await pool_refresh.run_refresh(db, dorg.tenant_id, rid)
    elapsed = time.monotonic() - started
    assert out["status"] == "done" and out["added"] == N
    assert elapsed < 120, elapsed
