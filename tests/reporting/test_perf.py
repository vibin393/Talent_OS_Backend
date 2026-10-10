"""Dashboard performance (FR-60: whole dashboard <= 3 s p95). Slow: run with ``-m slow``.

Seeds one tenant with 20k demands, 200k applications, 400k stage-history rows and 50k submissions. The rows
are generated server-side (``INSERT ... SELECT generate_series``, the bulk equivalent of ``COPY``) as the
owner role, then every manager tile is timed through the API (each < 1 s, sequential sum < 3 s) and the
tile SQL touching ``applications`` / ``submissions`` is EXPLAINed: no sequential scan on those tables.
"""

import time
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.pool import NullPool

from app.core.database import app_engine, owner_engine, tenant_session
from tests.reporting.helpers import API, ROrg, Seed

pytestmark = pytest.mark.slow

DEMANDS, APPLICATIONS, SUBMISSIONS = 20_000, 200_000, 50_000


async def _seed(org: ROrg) -> None:
    async with Seed(org.tenant_id) as s:
        client_id = await s.client(org.admin.user_id, "Perf Client")
        stage_ids = list(s.stages.values())
    tid, rec, bu_a, bu_b = org.tenant_id, org.recruiter.user_id, org.bu_a, org.bu_b
    engine = owner_engine(poolclass=NullPool)
    params: dict[str, Any] = {
        "t": tid,
        "rec": rec,
        "bua": bu_a,
        "bub": bu_b,
        "client": client_id,
        "stages": stage_ids,
    }
    statements = [
        f"""INSERT INTO demands (id, tenant_id, req_id, title, client_id, owner_id, status, positions, opened_at,
                business_unit_id)
            SELECT gen_random_uuid(), :t, 'PRF-' || g, 'Perf demand ' || g, :client, :rec,
                   (ARRAY['open','open','open','filled','on_hold'])[1 + g % 5], 1 + g % 3,
                   now() - (g % 365) * interval '1 day', CASE WHEN g % 2 = 0 THEN CAST(:bua AS uuid) ELSE CAST(:bub AS uuid) END
            FROM generate_series(1, {DEMANDS}) g""",
        """INSERT INTO candidates (id, tenant_id, human_id, full_name, source, owner_id, created_at)
            SELECT gen_random_uuid(), :t, 'PCN-' || g, 'Perf candidate ' || g, 'manual', :rec,
                   now() - (g % 700) * interval '1 day'
            FROM generate_series(1, 2000) g""",
        # Fresh statistics before the dependent bulk inserts: the FK (RI) checks of 200k / 400k child rows use
        # generic plans, and stale near-empty stats make them sequential scans (a quadratic seed).
        "ANALYZE demands", "ANALYZE candidates",
        """CREATE TEMP TABLE perf_d AS SELECT id, row_number() OVER (ORDER BY req_id) - 1 AS n FROM demands
            WHERE tenant_id = :t AND req_id LIKE 'PRF-%'""",
        """CREATE TEMP TABLE perf_c AS SELECT id, row_number() OVER (ORDER BY human_id) - 1 AS n FROM candidates
            WHERE tenant_id = :t AND human_id LIKE 'PCN-%'""",
        f"""INSERT INTO applications (id, tenant_id, human_id, candidate_id, demand_id, stage_id, stage_entered_at,
                source, owner_id, created_at)
            SELECT gen_random_uuid(), :t, 'PAP-' || g, c.id, d.id,
                   (CAST(:stages AS uuid[]))[1 + g % array_length(CAST(:stages AS uuid[]), 1)], now() - (g % 300) * interval '1 day',
                   (ARRAY['manual','search','career_portal','ai_match'])[1 + g % 4], :rec,
                   now() - (g % 360) * interval '1 day'
            FROM generate_series(0, {APPLICATIONS - 1}) g
            JOIN perf_d d ON d.n = g / 10
            JOIN perf_c c ON c.n = (g % 10) * 200 + (g / 10) % 200""",
        "ANALYZE applications",
        """INSERT INTO application_stage_history (id, tenant_id, application_id, from_stage_id, to_stage_id,
                actor_kind, at)
            SELECT gen_random_uuid(), a.tenant_id, a.id, NULL, (CAST(:stages AS uuid[]))[1], 'user', a.created_at
            FROM applications a WHERE a.tenant_id = :t AND a.human_id LIKE 'PAP-%'
            UNION ALL
            SELECT gen_random_uuid(), a.tenant_id, a.id, (CAST(:stages AS uuid[]))[1], a.stage_id, 'user', a.stage_entered_at
            FROM applications a WHERE a.tenant_id = :t AND a.human_id LIKE 'PAP-%'""",
        f"""INSERT INTO submissions (id, tenant_id, human_id, application_id, demand_id, client_id, candidate_id,
                status, pay_rate, bill_rate, currency, rate_unit, burden_pct, margin_amount, margin_pct,
                resume_kind, sent_at, sent_by)
            SELECT gen_random_uuid(), a.tenant_id, 'PSU-' || a.human_id, a.id, a.demand_id, :client, a.candidate_id,
                   'sent', 900, 1500, 'INR', 'hour', 18, 600, 30, 'original', a.created_at + interval '2 days', :rec
            FROM applications a WHERE a.tenant_id = :t AND a.human_id LIKE 'PAP-%'
            ORDER BY a.human_id LIMIT {SUBMISSIONS}""",
        "ANALYZE demands", "ANALYZE applications", "ANALYZE application_stage_history", "ANALYZE submissions",
        "ANALYZE candidates",
    ]  # fmt: skip
    async with engine.begin() as conn:
        for sql in statements:
            used = {k: v for k, v in params.items() if f":{k}" in sql}
            await conn.execute(text(sql), used)
    await engine.dispose()


CLEANUP = (
    "DELETE FROM submissions WHERE tenant_id = :t",
    "DELETE FROM application_stage_history WHERE tenant_id = :t",
    "DELETE FROM applications WHERE tenant_id = :t",
    "DELETE FROM demands WHERE tenant_id = :t",
    "DELETE FROM candidates WHERE tenant_id = :t",
)


async def _cleanup(org: ROrg) -> None:
    """Drop the bulk rows so repeated runs do not grow the shared test database."""
    engine = owner_engine(poolclass=NullPool)
    async with engine.begin() as conn:
        # Owner-only bulk delete: skip per-row FK checks (every child row is deleted explicitly above).
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        for sql in CLEANUP:
            await conn.execute(text(sql), {"t": org.tenant_id})
    await engine.dispose()


async def test_manager_dashboard_under_three_seconds(rorg: ROrg) -> None:
    await _seed(rorg)
    try:
        await _measure(rorg)
    finally:
        await _cleanup(rorg)


async def _measure(rorg: ROrg) -> None:
    listing = (await rorg.manager.client.get(f"{API}/dashboards")).json()["items"][0]
    captured: list[tuple[str, Any]] = []

    def capture(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if (
            "applications" in statement or "submissions" in statement
        ) and statement.lstrip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    engine = app_engine().sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    timings: dict[str, float] = {}
    statuses: dict[str, str] = {}
    try:
        params = {"refresh": "true", "business_unit_ids": str(rorg.bu_a)}
        # One warm-up pass (freshly bulk-loaded pages are cold); the second pass is measured (p95 regime).
        for t in listing["tiles"]:
            await rorg.manager.client.get(f"{API}/dashboards/manager/tiles/{t['key']}", params=params)
        captured.clear()
        for t in listing["tiles"]:
            started = time.perf_counter()
            resp = await rorg.manager.client.get(f"{API}/dashboards/manager/tiles/{t['key']}", params=params)
            timings[t["key"]] = time.perf_counter() - started
            assert resp.status_code == 200, resp.text
            statuses[t["key"]] = resp.json()["status"]
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert set(statuses.values()) == {"ok"}, f"{statuses} { {k: round(v, 3) for k, v in timings.items()} }"
    slow = {k: round(v, 3) for k, v in timings.items() if v >= 1.0}
    assert not slow, f"tiles over 1 s: {slow}; all: { {k: round(v, 3) for k, v in timings.items()} }"
    assert sum(timings.values()) < 3.0, timings

    seq_scans: list[str] = []
    async with tenant_session(rorg.tenant_id) as db:
        conn = await db.connection()
        for statement, parameters in captured:
            plan = await conn.exec_driver_sql("EXPLAIN " + statement, parameters)
            text_plan = "\n".join(str(r[0]) for r in plan)
            for table in ("applications", "submissions"):
                if f"Seq Scan on {table}" in text_plan:
                    seq_scans.append(f"{table}: {statement[:160]}")
    assert not seq_scans, "\n".join(seq_scans)
