"""Story 5.4: deterministic scoring (pure; no DB, no AI)."""

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.schemas.matching_profiles import (
    CandidateMatchProfile,
    CandidateSkillFact,
    DemandMatchProfile,
    RequiredSkill,
    SkillGraph,
)
from app.services.matching import scoring

TODAY = date(2026, 10, 1)
SPARK, PYSPARK, AZURE, ADF, DATABRICKS, SQL = (uuid.uuid4() for _ in range(6))
GRAPH = SkillGraph(parents={PYSPARK: SPARK, ADF: AZURE})
W = dict(scoring.DEFAULT_WEIGHTS)
FX = dict(scoring.DEFAULT_FX)


def cand(**overrides: Any) -> CandidateMatchProfile:
    base = CandidateMatchProfile(
        candidate_id=uuid.UUID(int=1),
        current_title="Data Engineer",
        skills=(
            CandidateSkillFact(DATABRICKS, "Databricks", Decimal(4)),
            CandidateSkillFact(PYSPARK, "PySpark", Decimal(3)),
        ),
        experience_years=Decimal(5),
        city="Chennai",
        state="Tamil Nadu",
        country="India",
        preferred_cities=(),
        willing_to_relocate=False,
        work_auth=("Indian citizen",),
        expected_comp_annual=Decimal(1_800_000),
        expected_comp_currency="INR",
        available_from=None,
        notice_days=30,
        domains=("banking",),
        role_titles=("Data Engineer",),
        employers=("HDFC",),
        certifications=(),
    )
    return dataclasses.replace(base, **overrides)


def demand(**overrides: Any) -> DemandMatchProfile:
    base = DemandMatchProfile(
        demand_id=uuid.UUID(int=2),
        title="Senior Data Engineer",
        skills=(
            RequiredSkill(DATABRICKS, "Databricks", True, Decimal(3)),
            RequiredSkill(SPARK, "Apache Spark", True, None),
            RequiredSkill(ADF, "ADF", True, None),
            RequiredSkill(SQL, "SQL", False, None),
        ),
        experience_min=Decimal(4),
        experience_max=Decimal(8),
        remote=False,
        city="Chennai",
        state="Tamil Nadu",
        country="IN",
        required_work_auth=(),
        budget_max_annual=Decimal(2_000_000),
        currency="INR",
        needed_by=TODAY + timedelta(days=45),
        domains=("banking", "insurance"),
    )
    return dataclasses.replace(base, **overrides)


def run(
    d: DemandMatchProfile | None = None,
    c: CandidateMatchProfile | None = None,
    sim: float | None = 0.5,
    **kw: Any,
) -> scoring.ScoreResult:
    return scoring.score(
        d or demand(),
        c or cand(),
        weights=kw.get("weights", W),
        graph=GRAPH,
        similarity=sim,
        fx=FX,
        today=TODAY,
    )


def test_missing_mandatory_caps_skills_and_is_listed() -> None:
    result = run()
    # Databricks exact (1.0), Spark via child PySpark (0.8), ADF missing, SQL missing (nice to have)
    assert result.missing_mandatory == ("ADF",)
    assert result.sub_scores["skills"] is not None
    assert result.sub_scores["skills"] <= 40  # 2 of 3 mandatory met -> cap floor(60 * 2/3) = 40
    assert any(e.text == "Mandatory skill 'ADF' missing" for e in result.evidence)


def test_skill_credit_exact_child_parent_and_years() -> None:
    only = lambda req: demand(skills=(req,))  # noqa: E731
    assert run(only(RequiredSkill(DATABRICKS, "Databricks", True))).sub_scores["skills"] == 100
    assert run(only(RequiredSkill(SPARK, "Apache Spark", True))).sub_scores["skills"] == 80  # child
    parent_holder = cand(skills=(CandidateSkillFact(SPARK, "Apache Spark", Decimal(2)),))
    assert (
        run(only(RequiredSkill(PYSPARK, "PySpark", False)), parent_holder).sub_scores["skills"] == 40
    )  # parent
    # years: 4 of 8 required -> half credit, and < 0.5 would count as missing
    assert run(only(RequiredSkill(DATABRICKS, "Databricks", False, Decimal(8)))).sub_scores["skills"] == 50
    weak = run(only(RequiredSkill(DATABRICKS, "Databricks", True, Decimal(10))))
    assert weak.missing_mandatory == ("Databricks",) and weak.sub_scores["skills"] == 0
    # label-only requirement (unresolved skill) matches by name
    assert run(only(RequiredSkill(None, "pyspark", True))).sub_scores["skills"] == 100
    assert run(demand(skills=())).sub_scores["skills"] is None


@pytest.mark.parametrize(
    ("years", "expected"),
    [
        (Decimal(5), 100),
        (Decimal(4), 100),
        (Decimal(8), 100),
        (Decimal(3), 75),
        (Decimal(0), 0),
        (Decimal(10), 80),
        (Decimal(20), 50),
    ],
)
def test_experience_boundaries(years: Decimal, expected: int) -> None:
    assert run(c=cand(experience_years=years)).sub_scores["experience"] == expected


def test_experience_unknown() -> None:
    assert run(c=cand(experience_years=None)).sub_scores["experience"] is None
    assert run(demand(experience_min=None, experience_max=None)).sub_scores["experience"] is None


def test_location_rules() -> None:
    assert run().sub_scores["location"] == 100  # same city
    assert run(demand(remote=True), cand(city="Delhi", country="India")).sub_scores["location"] == 100
    assert run(c=cand(city="Delhi", preferred_cities=("Chennai",))).sub_scores["location"] == 100
    assert run(c=cand(city="Delhi", willing_to_relocate=True)).sub_scores["location"] == 80
    assert run(c=cand(city="Madurai")).sub_scores["location"] == 60  # same state
    assert run(c=cand(city="Pune", state="Maharashtra")).sub_scores["location"] == 40  # IN == India
    assert run(c=cand(city="Austin", state="Texas", country="USA")).sub_scores["location"] == 0
    assert run(demand(required_work_auth=("H1B",))).sub_scores["location"] == 0
    assert run(demand(required_work_auth=("Indian citizen",))).sub_scores["location"] == 100
    assert run(c=cand(city=None, state=None, country=None)).sub_scores["location"] is None


def test_compensation_and_fx() -> None:
    assert run().sub_scores["compensation"] == 100
    over = run(c=cand(expected_comp_annual=Decimal(2_200_000)))  # 10 % over -> 60
    assert over.sub_scores["compensation"] == 60
    assert run(c=cand(expected_comp_annual=Decimal(4_000_000))).sub_scores["compensation"] == 0
    usd = run(c=cand(expected_comp_annual=Decimal(20_000), expected_comp_currency="USD"))  # 1.66M INR
    assert usd.sub_scores["compensation"] == 100
    assert run(c=cand(expected_comp_annual=None)).sub_scores["compensation"] is None
    assert run(c=cand(expected_comp_currency="XYZ")).sub_scores["compensation"] is None
    assert all(e.comp for e in over.evidence if e.kind == "compensation")


def test_availability() -> None:
    assert run().sub_scores["availability"] == 100  # notice 30 <= 45
    late = run(c=cand(notice_days=60))  # 15 days late -> 70
    assert late.sub_scores["availability"] == 70
    assert run(c=cand(available_from=TODAY + timedelta(days=100))).sub_scores["availability"] == 0
    assert run(c=cand(notice_days=None)).sub_scores["availability"] is None
    assert run(demand(needed_by=None)).sub_scores["availability"] is None


def test_domain_and_similarity() -> None:
    assert run().sub_scores["domain"] == 50
    assert run(demand(domains=())).sub_scores["domain"] is None
    assert run(sim=0.73).sub_scores["similarity"] == 73
    assert run(sim=-0.2).sub_scores["similarity"] == 0
    assert run(sim=None).sub_scores["similarity"] is None


def test_overall_redistributes_null_weights() -> None:
    subs = {
        "skills": 100,
        "experience": None,
        "location": 50,
        "compensation": None,
        "availability": None,
        "domain": None,
        "similarity": None,
    }
    # (35*100 + 10*50) / 45 = 88.9
    assert scoring.overall_of(subs, W) == Decimal("88.9")
    assert scoring.overall_of(dict.fromkeys(subs), W) == Decimal("0.0")
    result = run()
    assert Decimal(0) <= result.overall <= Decimal(100)
    assert result.overall == result.overall.quantize(Decimal("0.1"))


def test_weight_bounds() -> None:
    assert scoring.validate_weights({}) == W
    assert scoring.validate_weights({**W, "skills": 60, "domain": 0})["skills"] == 60
    for bad in ({"skills": 14}, {"domain": 61}, {"experience": -1}, {"nope": 3}, {"skills": "x"}):
        with pytest.raises(scoring.WeightsError):
            scoring.validate_weights({**W, **bad})
    with pytest.raises(scoring.WeightsError):
        scoring.validate_weights(dict.fromkeys(W, 0) | {"skills": 15, "experience": 0} | {"skills": 0})


def test_determinism_and_hash() -> None:
    a, b = run(), run()
    assert a == b
    h1 = scoring.input_hash(demand(), cand(), weights=W, similarity=0.5, fx=FX, today=TODAY)
    assert h1 == scoring.input_hash(demand(), cand(), weights=W, similarity=0.5, fx=FX, today=TODAY)
    assert h1 != scoring.input_hash(
        demand(), cand(notice_days=31), weights=W, similarity=0.5, fx=FX, today=TODAY
    )
    assert h1 != scoring.input_hash(
        demand(), cand(), weights={**W, "skills": 40}, similarity=0.5, fx=FX, today=TODAY
    )
