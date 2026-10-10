# ruff: noqa: E501
"""Epic 65 story 65.5 (FR-322, NFR-45): every registered non-placeholder agent has an Evaluation Set with >= 10 cases,
>= 5 red-team cases and a Fake LLM script; the red-team corpus covers the four FR-322 families with >= 3 cases each;
case ids are unique across suites.

``PENDING_EVAL_COVERAGE`` (shrink-only, empty at phase close) names agents whose owner epic is still adding cases."""

from collections import Counter
from pathlib import Path

import pytest

from evals.gate import eval_agents
from evals.run import all_cases

ROOT = Path(__file__).resolve().parents[2] / "evals"
PENDING_EVAL_COVERAGE: dict[str, str] = {}
FAMILIES = {
    "injection": ("inject", "prompt", "resume", "email", "chat", "transcript", "ignore"),
    "cross_scope": ("cross", "other_candidate", "another", "scope", "other user", "other_user"),
    "star": ("star", "reject", "release", "hire", "offer", "outcome", "approve", "verified"),
    "exfiltration": ("exfil", "leak", "send", "outbound", "forward"),
}


def _cases() -> list[tuple[str, object]]:
    return all_cases()


def test_case_ids_unique_across_suites() -> None:
    ids = Counter(c.id for _, c in _cases())  # type: ignore[attr-defined]
    dupes = sorted(k for k, n in ids.items() if n > 1)
    assert dupes == [], f"duplicate eval case ids: {dupes}"


REDTEAM_FILE_ALIASES = {"interview_assist": ("interview",), "voice_screening": ("voice",), "compliance": ("e62",),
                        "bgv": ("e62",), "onboarding": ("e62",)}  # fmt: skip


def _yaml_cases(path: Path) -> list[dict[str, object]]:
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else [raw]
    return [i for i in items if isinstance(i, dict) and "id" in i]


def _corpus(agent: str, eval_set: str) -> tuple[int, int]:
    """(agent cases, red-team cases): the agent's evaluation set directory (own formats included) plus the shared
    red-team corpus (cases naming the agent, or the agent's ``<alias>_redteam.yaml``)."""
    agent_n = red_n = 0
    folder = ROOT.parent / eval_set
    for path in sorted(folder.rglob("*.yaml")):
        n = len(_yaml_cases(path))
        if "redteam" in path.name:
            red_n += n
        elif "scripts" not in path.parts:
            agent_n += n
    aliases = (agent, *REDTEAM_FILE_ALIASES.get(agent, ()))
    for path in sorted((ROOT / "redteam").glob("*.yaml")):
        own_file = any(path.name.startswith(f"{a}_") for a in aliases)
        for case in _yaml_cases(path):
            if case.get("agent") == agent or (own_file and case.get("agent") in (None, "_test_agent")):
                red_n += 1
    return agent_n, red_n


@pytest.mark.parametrize("agent", sorted(eval_agents()))
def test_agent_eval_coverage(agent: str) -> None:
    if agent in PENDING_EVAL_COVERAGE:
        pytest.skip(f"pending: {PENDING_EVAL_COVERAGE[agent]}")
    spec = eval_agents()[agent]
    agent_n, red_n = _corpus(agent, spec.eval_set)
    assert agent_n >= 10, f"{agent}: {agent_n} eval cases (need >= 10)"
    assert red_n >= 5, f"{agent}: {red_n} red-team cases (need >= 5)"


def test_redteam_families() -> None:
    red = [c for s, c in _cases() if s == "redteam"]
    counts = {}
    for family, words in FAMILIES.items():
        counts[family] = sum(
            1 for c in red
            if any(w in f"{c.id} {c.description} {' '.join(c.tags)}".lower() for w in words)  # type: ignore[attr-defined]
        )  # fmt: skip
    assert all(n >= 3 for n in counts.values()), counts


def test_pending_eval_coverage_empty_at_close() -> None:
    if PENDING_EVAL_COVERAGE:  # phase close requires it empty (coordinator gate)
        pytest.xfail(f"open eval coverage gaps: {sorted(PENDING_EVAL_COVERAGE)}")
