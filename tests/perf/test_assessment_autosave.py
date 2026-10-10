"""Epic 40 A6 (NFR): autosave ``PUT .../attempt/answers`` stays under 300 ms at p95 (``-m slow`` or ``-m perf``; files under tests/perf need ``perf`` in the marker expression)."""

import time

import pytest

from tests.modules.assessments.helpers import (
    answer_for,
    invite_ok,
    link_token,
    make_candidate,
    save,
    simple_assessment,
    start,
)
from tests.modules.demands.helpers import DOrg

pytestmark = [pytest.mark.slow, pytest.mark.perf]
SAMPLES = 60


async def test_autosave_p95_under_300ms(dorg: DOrg) -> None:
    kinds = ("single", "multi", "numeric", "short_text", "code_output", "code_fill", "code_order", "code_find_bug", "essay")
    a, _ = await simple_assessment(dorg, kinds)
    cand = await make_candidate(dorg)
    await invite_ok(dorg, a["id"], cand)
    token = await link_token(dorg, cand)
    client = dorg.admin.client
    view = await start(client, token)
    session = view["session_id"]
    answers = [{"position": it["position"], "response": answer_for(it, it["kind"])} for it in view["items"]]
    timings: list[float] = []
    for seq in range(1, SAMPLES + 1):
        t0 = time.perf_counter()
        r = await save(client, token, session, seq, answers)
        timings.append(time.perf_counter() - t0)
        assert r.status_code == 200, r.text
    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    assert p95 < 0.300, f"autosave p95 {p95 * 1000:.0f} ms"
