# ruff: noqa: E501
"""Epic 49 A6: small-cell + complementary suppression (pure) -- unit cases and hypothesis properties.

Property (spec A6): for random tables (2-6 categories per dimension, counts 0-500, k 5-10), after suppression
* no shown known cell has ``0 < n < k`` (applicants, selected, not selected);
* for every suppressed cell and every measure, at least two non-negative integer completions are consistent with the
  published values (each table's total row = categories + unknown), unless the value follows from the published
  total and unknown count alone (the known part of the measure is 0);
* the unknown row is always shown and the snapshot carries no identifiers.
"""

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.fairness import aggregate, suppression
from app.services.fairness.aggregate import Dimension, Record
from app.services.fairness.reports import assert_no_ids
from app.services.fairness.suppression import Cell


def test_primary_rule() -> None:
    assert suppression.primary(Cell(4, 0), 5)
    assert suppression.primary(Cell(20, 3), 5)
    assert suppression.primary(Cell(20, 17), 5)  # 3 not selected
    assert not suppression.primary(Cell(20, 10), 5)
    assert not suppression.primary(Cell(0, 0), 5)  # empty cells disclose nobody
    assert not suppression.primary(Cell(3, 1, unknown=True), 5)  # unknown is never suppressed
    assert suppression.primary(Cell(20, 10, above=2), 5)


def test_category_with_four_applicants_gets_a_complementary_cell() -> None:
    tables = {
        "g": {
            "woman": Cell(4, 2),
            "man": Cell(50, 20),
            "non_binary": Cell(30, 12),
            "unknown": Cell(10, 4, unknown=True),
        }
    }
    hidden = suppression.apply(tables, [[("g", k) for k in tables["g"]]], 5)
    assert ("g", "woman") in hidden
    assert ("g", "non_binary") in hidden  # next-smallest known cell
    assert ("g", "unknown") not in hidden
    assert len(hidden) == 2


def test_unknown_is_never_the_complement() -> None:
    tables = {"g": {"a": Cell(3, 1), "b": Cell(100, 40), "unknown": Cell(2, 1, unknown=True)}}
    hidden = suppression.apply(tables, [[("g", "a"), ("g", "b"), ("g", "unknown")]], 5)
    assert hidden == {("g", "a"), ("g", "b")}


def test_zero_selected_among_suppressed_gets_another_cell() -> None:
    # a and b are suppressed with 0 selected; their 0 would follow from the total -> c is suppressed too.
    tables = {
        "g": {"a": Cell(3, 0), "b": Cell(4, 0), "c": Cell(40, 20), "d": Cell(60, 30), "unknown": Cell(5, 2, unknown=True)}
    }
    hidden = suppression.apply(tables, [[("g", k) for k in tables["g"]]], 5)
    assert {("g", "a"), ("g", "b"), ("g", "c")} <= hidden


def test_k_below_five_is_refused() -> None:
    with pytest.raises(ValueError, match="k must be at least 5"):
        suppression.apply({}, [], 4)


# ---------------------------------------------------------------- hypothesis properties
def _dims(n_g: int, n_a: int) -> list[Dimension]:
    return [
        Dimension("gender", "Gender", [(f"g{i}", f"G{i}") for i in range(n_g)]),
        Dimension("age_band", "Age band", [(f"a{i}", f"A{i}") for i in range(n_a)]),
    ]


@st.composite
def grids(draw: st.DrawFn) -> tuple[int, int, int, dict[tuple[int, int], tuple[int, int]]]:
    n_g = draw(st.integers(2, 6))
    n_a = draw(st.integers(2, 6))
    k = draw(st.integers(5, 10))
    small = st.one_of(st.integers(0, 12), st.integers(0, 500))
    grid: dict[tuple[int, int], tuple[int, int]] = {}
    for gi in range(n_g + 1):  # last index = unknown
        for ai in range(n_a + 1):
            n = draw(small)
            s = draw(st.integers(0, n))
            grid[gi, ai] = (n, s)
    return n_g, n_a, k, grid


def _records(n_g: int, n_a: int, grid: dict[tuple[int, int], tuple[int, int]]) -> list[Record]:
    out: list[Record] = []
    for (gi, ai), (n, s) in grid.items():
        cats = {"gender": f"g{gi}" if gi < n_g else None, "age_band": f"a{ai}" if ai < n_a else None}
        yes, no = Record(cats, True, Decimal(80)), Record(cats, False, Decimal(40))
        out += [yes] * s + [no] * (n - s)
    return out


def _check_table(table: dict, k: int) -> None:
    rows = table["rows"]
    unknown = [r for r in rows if r["unknown"]]
    assert len(unknown) == 1 and not unknown[0]["suppressed"]
    hidden = [r for r in rows if r["suppressed"]]
    shown = [r for r in rows if not r["suppressed"]]
    for r in shown:
        if r["unknown"]:
            continue
        n, s = r["applicants"], r["selected"]
        assert isinstance(n, int) and isinstance(s, int)
        assert not (0 < n < k) and not (0 < s < k) and not (0 < n - s < k), r
    for r in hidden:
        assert r["applicants"] == f"<{k}" and r["rate"] is None and r["impact_ratio"] is None and r["flag"] is None
    if not hidden:
        return
    assert len(hidden) >= 2, table["key"]
    total_n, total_s = table["totals"]["applicants"], table["totals"]["selected"]
    shown_n = sum(r["applicants"] for r in shown)
    shown_s = sum(r["selected"] for r in shown)
    u_n, u_s = unknown[0]["applicants"], unknown[0]["selected"]
    for measure_total, measure_shown, known_part in (
        (total_n, shown_n, total_n - u_n),
        (total_s, shown_s, total_s - u_s),
        (total_n - total_s, shown_n - shown_s, (total_n - total_s) - (u_n - u_s)),
    ):
        remainder = measure_total - measure_shown
        # >= 2 hidden cells and a positive remainder -> a unit can move between two hidden cells (two completions);
        # a zero remainder is only allowed when the known part is 0 anyway (follows from total and unknown).
        assert remainder > 0 or known_part == 0, (table["key"], remainder, known_part)


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(grids())
def test_no_suppressed_value_can_be_derived(case: tuple[int, int, int, dict[tuple[int, int], tuple[int, int]]]) -> None:
    n_g, n_a, k, grid = case
    snap = aggregate.build_snapshot(
        _records(n_g, n_a, grid), _dims(n_g, n_a), [("gender", "age_band")], k, score_based=False, category_set_version=1
    )
    assert_no_ids(snap)
    assert {t["key"] for t in snap["tables"]} == {"gender", "age_band", "gender×age_band"}
    for table in snap["tables"]:
        _check_table(table, k)
    # intersection rows / columns hold 0 or >= 2 suppressed cells (protective groups)
    inter = next(t for t in snap["tables"] if t["key"] == "gender×age_band")
    cells = {r["category"]: r for r in inter["rows"] if not r["unknown"]}
    for gi in range(n_g):
        row = [cells[f"g{gi}|a{ai}"]["suppressed"] for ai in range(n_a)]
        assert sum(row) != 1
    for ai in range(n_a):
        col = [cells[f"g{gi}|a{ai}"]["suppressed"] for gi in range(n_g)]
        assert sum(col) != 1


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(grids())
def test_score_based_tables_also_respect_k(case: tuple[int, int, int, dict[tuple[int, int], tuple[int, int]]]) -> None:
    n_g, n_a, k, grid = case
    snap = aggregate.build_snapshot(
        _records(n_g, n_a, grid), _dims(n_g, n_a), [], k, score_based=True, category_set_version=1
    )
    for table in snap["tables"]:
        _check_table(table, k)
        for r in table["rows"]:
            if r["suppressed"]:
                assert r["scoring_rate"] is None and r["scoring_impact_ratio"] is None
