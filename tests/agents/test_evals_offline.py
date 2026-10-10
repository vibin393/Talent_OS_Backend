# ruff: noqa: E501
"""Epic 53 offline evals and red-team corpus (AD-69): every YAML case under ``Backend/evals`` runs against the
test-only agent (or its owner agent) with the Fake LLM playing the model. Marker ``evals`` (``pytest -m evals``).

Epic 65: ``python -m evals.run`` drives this file (``EVALS_RESULTS_PATH`` / ``EVALS_AGENTS`` / ``EVALS_SUITE``); each
case's result is appended for the report, the store and the gate."""

import os

import pytest

from evals.run import all_cases, main, record_result, run_case, selected
from tests.agents.conftest import AgentOrg

CASES = all_cases()

pytestmark = pytest.mark.evals


def test_corpus_sizes() -> None:
    suites = [suite for suite, _ in CASES]
    assert suites.count("redteam") >= 12
    assert suites.count("foundation") >= 8
    assert len({case.id for _, case in CASES}) == len(CASES)


@pytest.mark.parametrize(("suite", "case"), CASES, ids=[f"{s}/{c.id}" for s, c in CASES])
async def test_case(agent_org: AgentOrg, suite: str, case: object) -> None:
    if not selected(suite, case):  # type: ignore[arg-type]
        pytest.skip("not selected by the evals CLI")
    problems = await run_case(case, agent_org)  # type: ignore[arg-type]
    record_result(suite, case, problems)  # type: ignore[arg-type]
    assert problems == [], f"{suite}/{case.id}: {problems}"  # type: ignore[attr-defined]


def test_live_dry_run_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY_LIVE", raising=False)
    assert "ANTHROPIC_API_KEY_LIVE" not in os.environ
    assert main(["--mode", "live", "--dry-run"]) == 0
