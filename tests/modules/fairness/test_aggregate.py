# ruff: noqa: E501, RUF001
"""Epic 49 A5/A11: adverse-impact tables for a fixture of 1,000 applicants -- rates, impact ratios, four-fifths flags,
LL144 scoring rate with the sample median, the unknown row always shown, no identifiers in the snapshot."""

from decimal import Decimal

import pytest

from app.services.fairness import aggregate
from app.services.fairness.aggregate import Dimension, Record
from app.services.fairness.reports import SnapshotLeakError, assert_no_ids

GENDER = Dimension("gender", "Gender", [("woman", "Woman"), ("man", "Man"), ("non_binary", "Non-binary")])
AGE = Dimension("age_band", "Age band", [("under_25", "Under 25"), ("25_34", "25-34"), ("35_44", "35-44")])


def _fixture() -> list[Record]:
    """woman 400 (120 selected), man 500 (200 selected), unknown gender 100 (30 selected). Ages split evenly; scores:
    selected 90, not selected 50 (median 50 -> 'above' == selected)."""
    out: list[Record] = []

    def add(gender: str | None, n: int, sel: int) -> None:
        for i in range(n):
            age = ("under_25", "25_34", "35_44")[i % 3]
            out.append(Record({"gender": gender, "age_band": age}, i < sel, Decimal(90 if i < sel else 50)))

    add("woman", 400, 120)
    add("man", 500, 200)
    add(None, 100, 30)
    return out


def _rows(snap: dict, key: str) -> dict[str, dict]:
    table = next(t for t in snap["tables"] if t["key"] == key)
    return {r["category"]: r for r in table["rows"]}


def test_rates_ratios_and_flags() -> None:
    records = _fixture()
    assert len(records) == 1000
    snap = aggregate.build_snapshot(records, [GENDER, AGE], [("gender", "age_band")], 5, score_based=True, category_set_version=3)
    g = _rows(snap, "gender")
    assert g["woman"]["applicants"] == 400 and g["woman"]["selected"] == 120
    assert g["woman"]["rate"] == "0.3000" and g["man"]["rate"] == "0.4000"
    assert g["man"]["impact_ratio"] == "1.0000" and g["man"]["flag"] is False
    assert g["woman"]["impact_ratio"] == "0.7500" and g["woman"]["flag"] is True  # four-fifths
    assert g["non_binary"]["applicants"] == 0 and g["non_binary"]["rate"] is None  # empty, shown
    assert g["unknown"]["applicants"] == 100 and g["unknown"]["impact_ratio"] is None  # always shown, no ratio
    # scoring rate: median 50 -> above = selected
    assert snap["median_score"] == "50.00"
    assert g["woman"]["scoring_rate"] == "0.3000" and g["woman"]["scoring_impact_ratio"] == "0.7500" and g["woman"]["scoring_flag"] is True
    assert snap["method"] == "both" and snap["category_set_version"] == 3
    assert snap["unknown_counts"] == {"gender": 100, "age_band": 0, "gender×age_band": 100}
    assert snap["total_applicants"] == 1000
    inter = _rows(snap, "gender×age_band")
    assert inter["woman|25_34"]["applicants"] == 133 or inter["woman|25_34"]["applicants"] == 134
    assert inter["unknown"]["applicants"] == 100
    assert_no_ids(snap)


def test_selection_rate_only_without_scores() -> None:
    snap = aggregate.build_snapshot(_fixture(), [GENDER], [], 5, score_based=False, category_set_version=1)
    assert snap["method"] == "selection_rate" and snap["median_score"] is None
    assert "scoring_rate" not in _rows(snap, "gender")["woman"]


def test_unknown_row_present_even_when_everyone_answered() -> None:
    records = [Record({"gender": "woman"}, i % 2 == 0) for i in range(20)] + [Record({"gender": "man"}, i % 2 == 0) for i in range(20)]
    g = _rows(aggregate.build_snapshot(records, [GENDER], [], 5, score_based=False, category_set_version=1), "gender")
    assert g["unknown"]["applicants"] == 0 and not g["unknown"]["suppressed"]


def test_snapshot_id_walker() -> None:
    with pytest.raises(SnapshotLeakError):
        assert_no_ids({"tables": [{"rows": [{"category": "0b5b2f0e-8f7c-4c1a-9a3e-2a1d1c0b9e77"}]}]})
    assert_no_ids({"tables": [{"rows": [{"category": "25_34", "applicants": 12}]}]})
