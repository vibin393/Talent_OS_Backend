# ruff: noqa: E501
"""Epic 49 A5 budget (``-m slow``): 12 months of AI Match for 500,000 candidates aggregates well inside the
``analytics`` task budget (< 5 min); the in-memory aggregation + suppression step is timed here."""

import time
from decimal import Decimal

import pytest

from app.services.fairness import aggregate
from app.services.fairness.aggregate import Dimension, Record

pytestmark = pytest.mark.slow


def test_500k_candidates_aggregate_within_budget() -> None:
    genders = ["woman", "man", "non_binary", None]
    ages = ["under_25", "25_34", "35_44", "45_54", "55_plus", None]
    proto = [
        Record({"gender": g, "age_band": a, "disability": d}, s, Decimal(sc))
        for g in genders for a in ages for d in ("yes", "no", None) for s, sc in ((True, 85), (False, 40))
    ]  # fmt: skip
    records = (proto * (500_000 // len(proto) + 1))[:500_000]
    dims = [
        Dimension("gender", "Gender", [("woman", "W"), ("man", "M"), ("non_binary", "N")]),
        Dimension("age_band", "Age", [(a, a) for a in ages if a]),
        Dimension("disability", "Disability", [("yes", "Y"), ("no", "N")]),
    ]
    t0 = time.perf_counter()
    snap = aggregate.build_snapshot(records, dims, [("gender", "age_band"), ("gender", "disability")], 5, score_based=True, category_set_version=1)
    elapsed = time.perf_counter() - t0
    assert snap["total_applicants"] == 500_000
    assert elapsed < 60, elapsed
