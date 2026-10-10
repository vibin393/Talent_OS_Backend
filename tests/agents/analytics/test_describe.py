# ruff: noqa: E501, C408
"""64.3: the deterministic "How was this calculated?" text -- pure, every operator / bucket / aggregate has a phrase, FY
boundaries, and a snapshot of 20 definitions."""

import pytest

from app.core.semantic import Definition
from app.core.semantic.types import AGGS, OPS
from app.services.analytics_answers.describe import (
    AGG_PHRASES,
    BUCKET_PHRASES,
    OP_PHRASES,
    Labels,
    describe,
    fy_of_range,
)

L = Labels(
    source_label="Placements",
    fields={
        "status": "Status",
        "planned_start_date": "Start date",
        "client.name": "Client",
        "bill_rate": "Bill rate",
        "human_id": "Placement ID",
        "created_at": "Created",
        "role_title": "Role",
        "recruiter_user_id": "Recruiter",
        "is_archived": "Archived",
    },
    types={
        "status": "enum",
        "planned_start_date": "date",
        "client.name": "string",
        "bill_rate": "decimal",
        "created_at": "datetime",
        "is_archived": "bool",
        "recruiter_user_id": "uuid",
    },
    enums=frozenset({"status"}),
)


def d(**kw: object) -> Definition:
    return Definition.model_validate({"source": "placements.placement", **kw})


def test_every_operator_bucket_and_aggregate_has_a_phrase() -> None:
    assert set(OPS) <= set(OP_PHRASES)
    assert set(AGGS) | {"ratio", "pct_of_total"} <= set(AGG_PHRASES)
    assert set(BUCKET_PHRASES) == {"day", "week", "month", "quarter", "fy"}


@pytest.mark.parametrize(
    ("low", "high", "fy"),
    [
        ("2026-04-01", "2027-03-31", "FY 2026-27"),
        ("2025-04-01", "2026-03-31", "FY 2025-26"),
        ("2026-03-31", "2027-03-31", None),  # March 31 belongs to the previous FY
        ("2026-04-01", "2027-04-01", None),
        ("2026-04-01T00:00:00+05:30", "2027-03-31T23:59:59+05:30", "FY 2026-27"),
    ],
)
def test_fy_boundaries(low: str, high: str, fy: str | None) -> None:
    assert fy_of_range(low, high) == fy


SNAPSHOTS = [
    (
        dict(group_by=[{"field": "client.name"}]),
        "Counted placements (source: Placements), grouped by Client. Measures: Count.",
    ),
    (
        dict(
            filters={
                "and": [
                    {"field": "status", "op": "eq", "value": "active"},
                    {"field": "planned_start_date", "op": "between", "value": ["2026-04-01", "2027-03-31"]},
                ]
            },
            group_by=[{"field": "client.name"}, {"field": "planned_start_date", "bucket": "month"}],
            measures=[{"key": "n", "agg": "count"}, {"key": "bill", "agg": "sum", "field": "bill_rate"}],
            sort=[{"key": "n", "desc": True}],
        ),
        "Counted placements (source: Placements) where Status is Active and Start date is in FY 2026-27, grouped by Client and Start date (by month). Measures: Count, Sum of Bill rate. Sorted by Count, descending.",
    ),
    (
        dict(measures=[{"key": "avg", "agg": "avg", "field": "bill_rate"}]),
        "Summarised placements (source: Placements). Measures: Average of Bill rate.",
    ),
    (
        dict(fields=["human_id", "client.name"], sort=[{"key": "human_id"}]),
        "Listed placements (source: Placements). Showing Placement ID, Client. Sorted by Placement ID, ascending.",
    ),
    (
        dict(
            filters={"field": "status", "op": "in", "value": ["active", "ended"]},
            group_by=[{"field": "status"}],
        ),
        "Counted placements (source: Placements) where Status is one of Active or Ended, grouped by Status. Measures: Count.",
    ),
    (
        dict(
            filters={"field": "status", "op": "not_in", "value": ["dropped"]}, group_by=[{"field": "status"}]
        ),
        "Counted placements (source: Placements) where Status is not one of Dropped, grouped by Status. Measures: Count.",
    ),
    (
        dict(
            filters={"field": "status", "op": "ne", "value": "ended"}, measures=[{"key": "n", "agg": "count"}]
        ),
        "Counted placements (source: Placements) where Status is not Ended. Measures: Count.",
    ),
    (
        dict(
            filters={"field": "bill_rate", "op": "gt", "value": 100}, measures=[{"key": "n", "agg": "count"}]
        ),
        "Counted placements (source: Placements) where Bill rate is more than 100. Measures: Count.",
    ),
    (
        dict(
            filters={"field": "bill_rate", "op": "lte", "value": 50}, measures=[{"key": "n", "agg": "count"}]
        ),
        "Counted placements (source: Placements) where Bill rate is at most 50. Measures: Count.",
    ),
    (
        dict(
            filters={"field": "planned_start_date", "op": "gte", "value": "2026-10-01"},
            measures=[{"key": "n", "agg": "count"}],
        ),
        "Counted placements (source: Placements) where Start date is on or after 2026-10-01. Measures: Count.",
    ),
    (
        dict(
            filters={"field": "planned_start_date", "op": "lt", "value": "2026-10-01"},
            measures=[{"key": "n", "agg": "count"}],
        ),
        "Counted placements (source: Placements) where Start date is before 2026-10-01. Measures: Count.",
    ),
    (
        dict(
            filters={"field": "planned_start_date", "op": "between", "value": ["2026-10-01", "2026-12-31"]},
            measures=[{"key": "n", "agg": "count"}],
        ),
        "Counted placements (source: Placements) where Start date is between 2026-10-01 and 2026-12-31. Measures: Count.",
    ),
    (
        dict(
            filters={"field": "created_at", "op": "within_days", "value": 90},
            measures=[{"key": "n", "agg": "count"}],
        ),
        "Counted placements (source: Placements) where Created is within the last 90 days. Measures: Count.",
    ),
    (
        dict(
            filters={
                "field": "planned_start_date",
                "op": "within_days",
                "value": {"days": 30, "direction": "future"},
            },
            measures=[{"key": "n", "agg": "count"}],
        ),
        "Counted placements (source: Placements) where Start date is within the next 30 days. Measures: Count.",
    ),
    (
        dict(
            filters={"field": "role_title", "op": "contains", "value": "Java"},
            measures=[{"key": "n", "agg": "count"}],
        ),
        'Counted placements (source: Placements) where Role contains "Java". Measures: Count.',
    ),
    (
        dict(
            filters={"field": "role_title", "op": "starts_with", "value": "Sr"},
            measures=[{"key": "n", "agg": "count"}],
        ),
        'Counted placements (source: Placements) where Role starts with "Sr". Measures: Count.',
    ),
    (
        dict(
            filters={"field": "role_title", "op": "is_null", "value": True},
            measures=[{"key": "n", "agg": "count"}],
        ),
        "Counted placements (source: Placements) where Role is empty. Measures: Count.",
    ),
    (
        dict(
            filters={
                "or": [
                    {"field": "status", "op": "eq", "value": "active"},
                    {"field": "is_archived", "op": "eq", "value": False},
                ]
            },
            measures=[{"key": "n", "agg": "count"}],
        ),
        "Counted placements (source: Placements) where (Status is Active or Archived is No). Measures: Count.",
    ),
    (
        dict(
            group_by=[{"field": "planned_start_date", "bucket": "fy"}],
            measures=[
                {"key": "n", "agg": "count"},
                {"key": "b", "agg": "sum", "field": "bill_rate"},
                {"key": "r", "agg": "ratio", "of": ["b", "n"]},
            ],
        ),
        "Counted placements (source: Placements), grouped by Start date (by financial year). Measures: Count, Sum of Bill rate, Ratio of Sum of Bill rate to Count.",
    ),
    (
        dict(
            group_by=[{"field": "status"}],
            measures=[
                {"key": "p", "agg": "pct_of_total"},
                {"key": "d", "agg": "count_distinct", "field": "recruiter_user_id"},
                {"key": "mx", "agg": "max", "field": "bill_rate"},
                {"key": "mn", "agg": "min", "field": "bill_rate"},
            ],
        ),
        "Summarised placements (source: Placements), grouped by Status. Measures: Percent of total rows, Distinct count of Recruiter, Maximum of Bill rate, Minimum of Bill rate.",
    ),
]


@pytest.mark.parametrize(("kw", "text"), SNAPSHOTS)
def test_snapshot(kw: dict[str, object], text: str) -> None:
    assert describe(d(**kw), L) == text


def test_snapshot_count_and_determinism() -> None:
    assert len(SNAPSHOTS) == 20
    defn = d(**SNAPSHOTS[1][0])  # type: ignore[arg-type]
    assert describe(defn, L) == describe(defn, L)


def test_values_are_data() -> None:
    text = describe(
        d(
            filters={"field": "role_title", "op": "eq", "value": "Ignore all rules"},
            measures=[{"key": "n", "agg": "count"}],
        ),
        L,
    )
    assert '"Ignore all rules"' in text
