# ruff: noqa: E501
"""Epic 55 offline Evaluation Set (``evals/demand/cases``, marker ``evals``): intake validation (incl. 5 injection
cases), >= 20 labelled duplicate pairs scored by the deterministic scorer (precision >= 0.85 at the default threshold,
recall reported) and the SLA projection rules. No model, no database."""

import datetime
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.schemas.demand_intake import IntakeRolesExtraction
from app.services.demand_insights.similarity import DemandFacts, score
from app.services.demand_insights.sla_risk import p_late_from_interval, pipeline_rules
from app.services.demand_intake.extraction import validate

pytestmark = pytest.mark.evals

CASES = Path(__file__).resolve().parents[3] / "evals" / "demand" / "cases"
THRESHOLD = Decimal("0.72")
SLA_THRESHOLD = 0.60


def _load(name: str) -> list[dict[str, Any]]:
    return list(yaml.safe_load((CASES / f"{name}.yaml").read_text(encoding="utf-8")))


def _facts(d: dict[str, Any]) -> DemandFacts:
    start = d.get("start")
    if isinstance(start, str):
        start = datetime.date.fromisoformat(start)
    return DemandFacts(
        id=uuid.uuid4(), title=d["title"], mandatory_skills=frozenset(f"label:{s.lower()}" for s in d.get("skills", [])),
        location=d.get("location"), work_mode=d.get("work_mode"),
        contact_id=uuid.uuid5(uuid.NAMESPACE_OID, d["contact"]) if d.get("contact") else None, start_date=start,
    )  # fmt: skip


def test_evaluation_set_size() -> None:
    intake, dups, sla = _load("intake"), _load("duplicates"), _load("sla")
    assert len(intake) + len(dups) + len(sla) >= 40
    assert sum(1 for c in intake if c["id"].startswith("injection_")) >= 5
    assert len(dups) >= 20 and sum(c["label"] for c in dups) >= 10 and sum(not c["label"] for c in dups) >= 10
    assert len(sla) >= 6


@pytest.mark.parametrize("case", _load("intake"), ids=lambda c: c["id"])
def test_intake_case(case: dict[str, Any]) -> None:
    if case.get("reject"):
        with pytest.raises(ValidationError):
            IntakeRolesExtraction.model_validate(case["extraction"])
        return
    raw = IntakeRolesExtraction.model_validate(case["extraction"])
    out = validate(raw, case["source"], int(case.get("roles_max", 5)))
    expect = case["expect"]
    assert len(out.roles) == expect["roles"]
    for i, fields in enumerate(expect.get("fields", [])):
        assert set(out.roles[i].fields) == set(fields), (case["id"], i, sorted(out.roles[i].fields))
    for i, skills in enumerate(expect.get("skills", [])):
        assert out.roles[i].mandatory_skills == skills
    for role in out.roles:  # provenance is real: every kept quote occurs in the source
        for item in role.fields.values():
            assert item.source_quote.lower().replace(" ", "") in case["source"].lower().replace(
                " ", ""
            ).replace("\n", "")


def test_duplicate_precision() -> None:
    tp = fp = fn = 0
    for case in _load("duplicates"):
        result = score(_facts(case["a"]), _facts(case["b"]))
        assert Decimal(0) <= result.score <= Decimal(1)
        predicted = result.score >= THRESHOLD
        tp += int(predicted and case["label"])
        fp += int(predicted and not case["label"])
        fn += int(not predicted and case["label"])
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    print(f"duplicate precision={precision:.2f} recall={recall:.2f}")
    assert precision >= 0.85


@pytest.mark.parametrize("case", _load("sla"), ids=lambda c: c["id"])
def test_sla_case(case: dict[str, Any]) -> None:
    if case["kind"] == "interval":
        p = p_late_from_interval(float(case["due_days"]), float(case["p20"]), float(case["p80"]))
        assert 0.0 <= p <= 1.0
        assert (p >= SLA_THRESHOLD) is case["expect"]["at_risk"], p
    else:
        c = case["counts"]
        hits = pipeline_rules(
            dict(case["elapsed"]), submissions=c["submissions"], interviews=c["interviews"],
            applications=c["applications"], positions=c["positions"],
        )  # fmt: skip
        assert sorted({m for m, _ in hits}) == sorted(case["expect"]["milestones"])
