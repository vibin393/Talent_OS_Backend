# ruff: noqa: E501
"""Epic 55 stories 55.4 / 55.5 (pure): the deterministic duplicate scorer (weights, embedding redistribution, score in
[0, 1], reasons) and the SLA projection helpers. No database."""

import random
import uuid
from datetime import date
from decimal import Decimal

from app.services.demand_insights.similarity import DemandFacts, city, score, summary_text, trigram_similarity
from app.services.demand_insights.sla_risk import p_late_from_interval, pipeline_rules

C1 = uuid.uuid4()


def facts(**over: object) -> DemandFacts:
    base: dict[str, object] = {
        "id": uuid.uuid4(), "title": "Senior Data Engineer", "mandatory_skills": frozenset({"global:a", "global:b", "label:sql"}),
        "location": "Pune", "work_mode": "hybrid", "contact_id": C1, "start_date": date(2026, 11, 1),
    }  # fmt: skip
    base.update(over)
    return DemandFacts(**base)  # type: ignore[arg-type]


def test_identical_demands_score_one_with_reasons() -> None:
    out = score(facts(), facts())
    assert out.score == Decimal("1.000")
    assert [r.field for r in out.reasons] == [
        "title",
        "skills",
        "location",
        "work_mode",
        "hiring_contact",
        "start_date",
    ]
    assert summary_text("REQ-1041", out.reasons).startswith(
        "REQ-1041 looks similar: same title, 3 of 3 skills"
    )


def test_weights_without_embedding_are_redistributed() -> None:
    # only the title and dates match: (0.30 + 0.05) / 0.85
    a = facts(mandatory_skills=frozenset({"label:x"}), location="Delhi", work_mode="onsite", contact_id=None)
    b = facts(
        mandatory_skills=frozenset({"label:y"}), location="Pune", work_mode="remote", contact_id=uuid.uuid4()
    )
    assert score(a, b).score == Decimal("0.412")


def test_embedding_weight_used_when_both_vectors_exist() -> None:
    a = facts(vector=(1.0, 0.0), contact_id=None)
    b = facts(vector=(0.0, 1.0), contact_id=None)
    # everything matches except the contact (0.10) and the embedding (cosine 0 -> 0.15)
    assert score(a, b).score == Decimal("0.750")


def test_city_normalisation() -> None:
    assert city("Bangalore, Karnataka") == city("Bengaluru") == "bengaluru"
    assert city("Gurgaon") == "gurugram" and city(None) is None


def test_trigram_similarity_bounds() -> None:
    assert trigram_similarity("Data Engineer", "data engineer") == 1.0
    assert trigram_similarity("", "x") == 0.0
    assert 0.0 < trigram_similarity("Sr. Data Engineer", "Senior Data Engineer") < 1.0


def test_score_always_in_unit_interval() -> None:
    rng = random.Random(55)
    titles = ["Data Engineer", "QA Lead", "Scrum Master", "", "Senior Data Engineer", "Java Developer"]
    for _ in range(300):

        def pick() -> DemandFacts:
            return facts(
                title=rng.choice(titles), mandatory_skills=frozenset(rng.sample(["label:a", "label:b", "label:c", "label:d"], rng.randint(0, 4))),
                location=rng.choice([None, "Pune", "Delhi"]), work_mode=rng.choice([None, "hybrid", "remote"]),
                contact_id=rng.choice([None, C1]), start_date=rng.choice([None, date(2026, 11, 1), date(2027, 2, 1)]),
                vector=rng.choice([None, (rng.random(), rng.random())]),
            )  # fmt: skip

        value = score(pick(), pick()).score
        assert Decimal(0) <= value <= Decimal(1)


def test_p_late_interval_and_pipeline_rules() -> None:
    assert p_late_from_interval(10, 20, 40) == 1.0
    assert p_late_from_interval(60, 20, 40) == 0.0
    assert abs(p_late_from_interval(30, 20, 40) - 0.5) < 1e-9
    hits = pipeline_rules({"first_submission": 50}, submissions=0, interviews=0, applications=10, positions=2)
    assert [m for m, _ in hits] == ["first_submission"]
    assert (
        pipeline_rules({"first_submission": 49}, submissions=0, interviews=0, applications=10, positions=2)
        == []
    )
