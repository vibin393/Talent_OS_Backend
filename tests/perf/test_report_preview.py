# ruff: noqa: E501
"""Epic 47 (47.2, NFR-39): a 500-row custom-report preview answers at p95 < 5 s on a seeded tenant (``-m slow``)."""

import statistics
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from sqlalchemy import insert, text
from sqlalchemy.pool import NullPool

from app.core.database import owner_engine, tenant_session
from app.models.placements import Placement, RateSegment
from app.services.reporting.builder import hooks as builder_hooks
from tests.reporting.helpers import h, make_rorg
from tests.reporting.p2 import seed as S

pytestmark = pytest.mark.slow

N_PLACEMENTS = 5_000
RUNS = 10


async def test_report_preview_p95_under_5s(app: FastAPI) -> None:
    builder_hooks.register()
    org = await make_rorg(app)
    try:
        t = org.tenant_id
        async with tenant_session(t) as db:
            le = await S.legal_entity(db, t)
            clients = [await S.client(db, t, org.account.user_id, f"Perf {i}") for i in range(20)]
            cand = await S.candidate(db, t, org.recruiter.user_id)
            placements, rates = [], []
            for i in range(N_PLACEMENTS):
                pid = uuid.uuid4()
                start = date(2025, 4, 1) + timedelta(days=i % 700)
                placements.append({"id": pid, "tenant_id": t, "human_id": f"PRF-{i:06d}", "candidate_id": cand, "client_id": clients[i % 20],
                                   "legal_entity_id": le, "payee_kind": "worker", "status": "active", "planned_start_date": start,
                                   "currency": "INR", "recruiter_user_id": org.recruiter.user_id, "account_manager_user_id": org.account.user_id})  # fmt: skip
                rates.append({"id": uuid.uuid4(), "tenant_id": t, "placement_id": pid, "effective_from": start, "rate_unit": "hour",
                              "currency": "INR", "bill_rate": Decimal(100 + i % 50), "pay_rate": Decimal(80 + i % 40), "status": "active"})  # fmt: skip
            await db.execute(insert(Placement), placements)
            await db.execute(insert(RateSegment), rates)
        # Steady state: the planner has statistics (autovacuum would have analyzed a production table).
        engine = owner_engine(poolclass=NullPool)
        async with engine.begin() as conn:  # committed: ANALYZE in a rolled-back transaction keeps no stats
            await conn.execute(text("ANALYZE placements, rate_segments, clients"))
        await engine.dispose()
        admin = org.admin
        bodies = [
            {"definition": {"source": "placements.placement", "fields": ["human_id", "client.name", "planned_start_date", "bill_rate", "pay_rate", "status"]}, "viz": {"type": "table"}},
            {"definition": {"source": "placements.placement", "group_by": [{"field": "client.name"}, {"field": "planned_start_date", "bucket": "month"}],
                            "measures": [{"key": "n", "agg": "count"}, {"key": "bill", "agg": "sum", "field": "bill_rate"}, {"key": "pay", "agg": "sum", "field": "pay_rate"},
                                         {"key": "ratio", "agg": "ratio", "of": ["bill", "pay"]}]}, "viz": {"type": "bar", "x": "planned_start_date", "series": "client.name", "value": "bill"}},
        ]  # fmt: skip
        timings = []
        for i in range(RUNS):
            started = time.perf_counter()
            resp = await admin.client.post("/api/v1/reports/custom/preview", json=bodies[i % 2], headers=h(admin))
            timings.append(time.perf_counter() - started)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["limit_hit"] is None and len(data["rows"]) == (500 if i % 2 == 0 else 461)  # 20 clients x 23 months
        p95 = statistics.quantiles(timings, n=20)[-1]
        assert p95 < 5.0, timings
        print(f"report preview p95 = {p95:.3f}s over {RUNS} runs")
    finally:
        await org.close()
