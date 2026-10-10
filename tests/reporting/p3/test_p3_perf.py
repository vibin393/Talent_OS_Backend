# ruff: noqa: E501
"""Epic 52 (A4): Phase 3 dashboard performance (``-m slow``). Every Phase 3 dashboard with all tiles requested in
parallel on a cold cache must answer in p95 < 3 s.

Seed (scaled by ``E52_PERF_SCALE``, default 1.0 = spec volumes where the table can be bulk-seeded with SQL alone):
500 projects with cost budgets / burn, 5,000 referrals, predictions for 10,000 subjects. Campaign messages, campus
registrations and immigration cases need owner graphs (audiences, drives / institutions, visa type versions) and are
measured on the data present; the gap is recorded in ``deferred-work.md`` (never silently passed).
"""

import asyncio
import os
import statistics
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.reporting.dashboards_p3 import P3_DASHBOARDS, P3_ORDER
from tests.modules.demands.helpers import DOrg

pytestmark = pytest.mark.slow
SCALE = float(os.environ.get("E52_PERF_SCALE", "1.0"))
P95_BUDGET_S = 3.0
ROUNDS = 5


async def _seed(org: DOrg, owner_db: AsyncEngine) -> None:
    t = str(org.tenant_id)
    n_projects, n_referrals, n_predictions = int(500 * SCALE), int(5000 * SCALE), int(10000 * SCALE)
    model_id = str(uuid.uuid4())
    async with owner_db.begin() as conn:
        await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t})
        await conn.execute(
            text(
                "INSERT INTO projects (id, tenant_id, human_id, name, client_id, status, currency, created_at, updated_at) "
                "SELECT gen_random_uuid(), CAST(:t AS uuid), 'PRF-' || g, 'Perf project ' || g, CAST(:c AS uuid), "
                "'active', 'INR', now(), now() FROM generate_series(1, :n) g"
            ),
            {"t": t, "c": str(org.client_id), "n": n_projects},
        )
        await conn.execute(
            text(
                "INSERT INTO project_budgets (id, tenant_id, project_id, dimension, amount, currency, version, created_at, updated_at) "
                "SELECT gen_random_uuid(), tenant_id, id, 'cost', 100000, 'INR', 1, now(), now() FROM projects WHERE tenant_id = CAST(:t AS uuid)"
            ),
            {"t": t},
        )
        await conn.execute(
            text(
                "INSERT INTO project_burn (id, tenant_id, project_id, dimension, actual, budget, pct, burn_rate_per_day, "
                "forecast_at_completion, currency, as_of, breakdown, source_hash, created_at, updated_at) "
                "SELECT gen_random_uuid(), tenant_id, id, 'cost', 85000, 100000, 85, 100, 120000, 'INR', current_date, "
                "'{}'::jsonb, repeat('0', 64), now(), now() FROM projects WHERE tenant_id = CAST(:t AS uuid)"
            ),
            {"t": t},
        )
        await conn.execute(
            text(
                "INSERT INTO referrals (id, tenant_id, human_id, referrer_kind, referrer_user_id, channel, status, created_at, updated_at) "
                "SELECT gen_random_uuid(), CAST(:t AS uuid), 'RFP-' || g, 'user', CAST(:u AS uuid), 'upload', "
                "(ARRAY['submitted','accepted','in_process','hired','not_selected'])[1 + g % 5], now(), now() "
                "FROM generate_series(1, :n) g"
            ),
            {"t": t, "n": n_referrals, "u": str(org.admin.user_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO ml_models (id, tenant_id, kind, version, status, n_samples, seed, feature_spec_version, "
                "metrics, params, created_at, updated_at) VALUES (CAST(:m AS uuid), CAST(:t AS uuid), 'attrition', 7001, "
                "'superseded', 0, 1, 1, '{}'::jsonb, '{}'::jsonb, now(), now())"
            ),
            {"t": t, "m": model_id},
        )
        await conn.execute(
            text(
                "INSERT INTO ml_predictions (id, tenant_id, model_id, kind, subject_kind, subject_id, score, band, "
                "contributions, explanation_status, computed_at) SELECT gen_random_uuid(), CAST(:t AS uuid), "
                "CAST(:m AS uuid), 'attrition', 'placement', gen_random_uuid(), 0.5, "
                "(ARRAY['low','medium','high'])[1 + g % 3], '[]'::jsonb, 'none', now() FROM generate_series(1, :n) g"
            ),
            {"t": t, "m": model_id, "n": n_predictions},
        )
        for table in ("projects", "project_budgets", "project_burn", "referrals", "ml_predictions"):
            await conn.execute(text(f"ANALYZE {table}"))


async def _dashboard(client: Any, key: str) -> float:
    start = time.perf_counter()
    resps = await asyncio.gather(
        *(
            client.get(f"/api/v1/dashboards/{key}/tiles/{tile.key}", params={"refresh": "true"})
            for tile in P3_DASHBOARDS[key].tiles
        )
    )
    elapsed = time.perf_counter() - start
    for r in resps:
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok", (key, r.json())
    return elapsed


async def test_p3_dashboards_p95_under_3s(dorg: DOrg, owner_db: AsyncEngine) -> None:
    await _seed(dorg, owner_db)
    results: dict[str, float] = {}
    for key in P3_ORDER:
        times = sorted([await _dashboard(dorg.admin.client, key) for _ in range(ROUNDS)])
        results[key] = times[min(len(times) - 1, round(0.95 * (len(times) - 1)))]
    print(
        "E52 P3 dashboard p95 (s):",
        {k: round(v, 3) for k, v in results.items()},
        "median:",
        round(statistics.median(results.values()), 3),
    )
    slow = {k: round(v, 3) for k, v in results.items() if v >= P95_BUDGET_S}
    assert slow == {}, f"Phase 3 dashboards over {P95_BUDGET_S}s p95 (record in deferred-work.md): {slow}"
