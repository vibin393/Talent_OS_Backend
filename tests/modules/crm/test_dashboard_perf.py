# ruff: noqa: E501
"""Epic 15 A7 perf check (``-m perf``): the sales dashboard over 20k opportunities / 50k leads answers within 3 s
(p95 of 5 calls) for a tenant-wide manager."""

import statistics
import time
import uuid
from datetime import date

import pytest
from sqlalchemy import text

from app.core.database import tenant_session
from tests.modules.crm.conftest import API, CrmOrg, create_client, ok

pytestmark = pytest.mark.perf


async def test_dashboard_p95_under_3s(corg: CrmOrg, request: pytest.FixtureRequest) -> None:
    if "perf" not in (request.config.getoption("markexpr") or "") or "not perf" in request.config.getoption("markexpr"):
        pytest.skip("seeds 70k rows: run with -m perf")
    client = await create_client(corg.admin, "Perf Co")
    owner = corg.admin.user_id
    async with tenant_session(corg.tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, human_id, name, client_id, service, estimated_revenue, currency, "
                "probability_pct, owner_id, account_manager_id, stage, expected_close_date, won_at, lost_at, created_at) "
                "SELECT gen_random_uuid(), :t, 'OPP-P' || g, 'Deal ' || g, :c, 'contract_staffing', (g % 1000) * 100, "
                "CASE WHEN g % 7 = 0 THEN 'USD' ELSE 'INR' END, 40, :o, :o, "
                "(ARRAY['qualification','proposal','negotiation','won','lost'])[1 + g % 5], "
                "CAST(:d AS date) + (g % 200 - 50), CASE WHEN g % 5 = 3 THEN now() - (g % 60) * interval '1 day' END, "
                "CASE WHEN g % 5 = 4 THEN now() - (g % 60) * interval '1 day' END, now() - (g % 120) * interval '1 day' "
                "FROM generate_series(1, 20000) g"
            ),
            {"t": corg.tenant_id, "c": uuid.UUID(client["id"]), "o": owner, "d": date.today()},
        )
        await db.execute(
            text(
                "INSERT INTO leads (id, tenant_id, human_id, company_name, owner_id, status, source_code, created_at) "
                "SELECT gen_random_uuid(), :t, 'LEAD-P' || g, 'Perf Lead ' || g, :o, "
                "(ARRAY['new','contacted','qualified','converted','disqualified'])[1 + g % 5], "
                "(ARRAY['website','referral','linkedin','event'])[1 + g % 4], now() - (g % 90) * interval '1 day' "
                "FROM generate_series(1, 50000) g"
            ),
            {"t": corg.tenant_id, "o": owner},
        )
    timings = []
    for _ in range(5):
        start = time.perf_counter()
        body = await ok(await corg.admin.client.get(f"{API}/crm/dashboard"))
        timings.append(time.perf_counter() - start)
    assert sum(s["count"] for s in body["pipeline"]["by_stage"]) > 0
    p95 = statistics.quantiles(timings, n=20)[-1] if len(timings) > 1 else timings[0]
    assert p95 <= 3.0, timings
