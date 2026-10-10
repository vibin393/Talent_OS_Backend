"""NFR-3: candidate search p95 < 500 ms over 30 mixed queries with 100k projection rows (``-m perf``)."""

import statistics
import time
from typing import Any

import pytest

from tests.epic5 import GLOBAL, search
from tests.epic5_seed import purge, seed
from tests.modules.demands.helpers import DOrg

pytestmark = pytest.mark.perf

QUERIES: list[dict[str, Any]] = [
    {"q": "databricks"},
    {"q": "data engineer"},
    {"q": '"data engineer" AND pyspark'},
    {"q": "(java OR selenium) NOT databricks"},
    {"q": "seed person 4242"},
    {"q": "engin*"},
    {"q": "sql", "filters": {"cities": ["Chennai"]}},
    {"q": "java", "filters": {"experience_min": 3, "experience_max": 8}},
    {"filters": {"notice_max_days": 30}},
    {"filters": {"cities": ["Pune", "Hyderabad"]}, "sort": "experience"},
    {"q": "power", "sort": "updated"},
    {"q": "databricks", "page": 5},
    {"filters": {"sources": ["manual"]}},
    {"q": "qa engineer selenium"},
    {"q": "Employer 7"},
]


async def test_search_p95_under_500ms(dorg: DOrg) -> None:
    await seed(dorg.tenant_id, dorg.admin.user_id, 100_000, with_embeddings=False)
    try:
        await _measure(dorg)
    finally:  # keep the shared test database small
        await purge(dorg.tenant_id)


async def _measure(dorg: DOrg) -> None:
    m = dorg.admin
    await search(m.client, q="warmup")
    timings: list[float] = []
    for body in QUERIES * 2:  # 30 queries
        started = time.perf_counter()
        resp = await search(m.client, **body)
        timings.append((time.perf_counter() - started) * 1000)
        assert resp.status_code == 200, resp.text
    p95 = statistics.quantiles(timings, n=20)[18]
    g_timings = []
    for q in ("seed person 12", "SEED-0004242", "data"):
        started = time.perf_counter()
        resp = await m.client.get(GLOBAL, params={"q": q})
        g_timings.append((time.perf_counter() - started) * 1000)
        assert resp.status_code == 200
    print(f"search p95={p95:.0f} ms max={max(timings):.0f} ms; global max={max(g_timings):.0f} ms")
    assert p95 < 500
    assert max(g_timings) < 1000
