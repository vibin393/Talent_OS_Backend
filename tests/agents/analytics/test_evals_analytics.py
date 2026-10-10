# ruff: noqa: E501
"""Epic 64 story 64.4 (FR-322): the Analytics Evaluation Set (``evals/analytics/cases``, >= 100 questions over every
registered semantic source) and its comparator. Offline (``-m evals``) the Fake LLM plays the gold Definition through the
real runtime, tools, validation and scope -- this proves plumbing and containment, not model quality (Epic 65's live mode
gates >= 90 % correct Definitions with ``eval_threshold=0.9``)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.hooks import autodiscover
from app.core.semantic import sources, validate
from app.schemas.report_builder import VizSpec
from app.services.reporting.builder.validate import check_viz
from evals.analytics.compare import canonical, matches
from tests.agents.analytics.helpers import enable, run_agent, run_state
from tests.reporting.helpers import ROrg

pytestmark = pytest.mark.evals

ROOT = Path(__file__).resolve().parents[3] / "evals" / "analytics" / "cases"
CASES: list[dict[str, Any]] = [
    c for p in sorted(ROOT.glob("*.yaml")) for c in yaml.safe_load(p.read_text(encoding="utf-8"))
]
ANSWERABLE = [c for c in CASES if "definition" in c["expected"]]
REQUIRED_TAGS = {
    "fy",
    "quarter",
    "boundary",
    "ratio",
    "pct_of_total",
    "top_n",
    "by_recruiter",
    "by_client",
    "this_month",
    "masked",
    "ambiguity",
    "unanswerable",
    "injection",
    "clarify",
}


def test_case_count_and_coverage() -> None:
    autodiscover("app.services")
    assert len(CASES) >= 100
    assert len({c["id"] for c in CASES}) == len(CASES)
    covered = {c["expected"]["definition"]["source"] for c in ANSWERABLE}
    assert set(sources()) <= covered, set(sources()) - covered
    tags = {t for c in CASES for t in c.get("tags", [])}
    assert tags >= REQUIRED_TAGS, REQUIRED_TAGS - tags
    for c in CASES:
        assert c["persona"] in {"admin", "recruiter"} and c["question"].strip()
        exp = c["expected"]
        assert sum(k in exp for k in ("definition", "clarify", "unanswerable")) == 1, c["id"]


@pytest.mark.parametrize("case", ANSWERABLE, ids=[c["id"] for c in ANSWERABLE])
def test_gold_definitions_are_valid(case: dict[str, Any]) -> None:
    autodiscover("app.services")
    defn = validate(case["expected"]["definition"])
    check_viz(VizSpec.model_validate(case["expected"]["viz"]), defn)


def test_comparator() -> None:
    gold = {
        "source": "s",
        "filters": {
            "and": [{"field": "a", "op": "in", "value": [2, 1]}, {"field": "b", "op": "eq", "value": "x"}]
        },
        "fields": [],
        "group_by": [{"field": "d", "bucket": "day"}],
        "measures": [
            {"key": "n", "agg": "count"},
            {"key": "bill", "agg": "sum", "field": "f"},
            {"key": "r", "agg": "ratio", "of": ["bill", "n"]},
        ],
        "sort": [{"key": "n", "desc": True}],
    }
    same = {
        "source": "s",
        "filters": {
            "and": [
                {"field": "b", "op": "eq", "value": "x"},
                {"and": [{"field": "a", "op": "in", "value": [1, 2, 2]}]},
            ]
        },
        "group_by": [{"field": "d"}],
        "measures": [
            {"key": "total", "agg": "sum", "field": "f"},
            {"key": "cnt", "agg": "count"},
            {"key": "rr", "agg": "ratio", "of": ["total", "cnt"]},
        ],
        "sort": [{"key": "cnt", "desc": True}],
    }
    assert matches(same, gold, {"day_fields": ["d"]})
    assert not matches(same, gold)  # bucket None == day only when declared
    assert not matches({**same, "sort": []}, gold, {"day_fields": ["d"]})
    assert matches({**same, "sort": []}, gold, {"day_fields": ["d"], "ignore_sort": True})
    other = {**gold, "filters": {"field": "b", "op": "eq", "value": "y"}}
    assert not matches(other, gold)
    assert canonical({"source": "s", "fields": ["b", "a"]}) == canonical(
        {"source": "s", "fields": ["a", "b"]}
    )


async def test_offline_cases_through_the_runtime(rorg: ROrg) -> None:
    """Every answerable case: the Fake plays ``semantic.run`` with the gold Definition; the stored answer's definition
    matches the gold one, restricted personas get masked values, and nothing but read tools ran."""
    await enable(rorg.tenant_id)
    problems: list[str] = []
    for case in ANSWERABLE:
        exp = case["expected"]
        member = rorg.admin if case["persona"] == "admin" else rorg.recruiter
        args: dict[str, Any] = {
            "question": case["question"],
            "definition": exp["definition"],
            "viz": exp["viz"],
        }
        if exp.get("assumptions"):
            args["assumptions"] = exp["assumptions"]
        script = [
            {"kind": "tool_calls", "calls": [["semantic.run", args]]},
            {"kind": "text", "text": "Done."},
        ]
        run_id = await run_agent(rorg.tenant_id, member.user_id, script)
        run, calls, answers = await run_state(rorg.tenant_id, run_id)
        if run.status != "completed" or len(answers) != 1:
            problems.append(
                f"{case['id']}: {run.status} {[(c.tool_key, c.outcome, c.error_code) for c in calls]}"
            )
            continue
        a = answers[0]
        if not matches(a.definition, exp["definition"], case.get("tolerance")):
            problems.append(f"{case['id']}: definition mismatch")
        if "masked" in case.get("tags", []) and case["persona"] == "recruiter" and not a.masked_fields:
            problems.append(f"{case['id']}: expected masked fields")
        if exp.get("assumptions") and a.assumptions != exp["assumptions"]:
            problems.append(f"{case['id']}: assumptions not stored")
    assert problems == []
