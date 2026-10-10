"""Epic 78 A7: the campaign copy evaluation set (>= 40 cases, offline). Gate 0.9, red-team 100 %."""

import pytest

from evals.campaign_copy.runner import GATE, load, run

pytestmark = pytest.mark.evals


def test_case_set_is_large_and_covers_the_red_team() -> None:
    cases = load()
    tags = {t for c in cases for t in c.get("tags") or []}
    assert len(cases) >= 40
    assert {"golden", "redteam", "schema", "email", "sms", "whatsapp"} <= tags


def test_gate_and_red_team() -> None:
    result = run()
    assert result["score"] >= GATE, result["failures"]
    assert result["redteam_rate"] == 1.0, result["failures"]
