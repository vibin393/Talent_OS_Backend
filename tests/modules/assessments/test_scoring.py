# ruff: noqa: E501
"""Epic 40 A7: pure scoring (unit table per kind + hypothesis properties) and the pure draw (A4)."""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.assessments import draw, scoring
from app.services.assessments.scoring import score_answer

D = Decimal
KEYS: dict[str, dict[str, Any]] = {
    "single": {"correct": "b"},
    "true_false": {"correct": "t"},
    "multi": {"correct": ["a", "c"]},
    "numeric": {"value": "3.14", "tolerance_abs": "0.01"},
    "short_text": {"accepted": ["Paris"], "normalise": {"case": True, "whitespace": True, "punctuation": True}, "keywords": [{"term": "capital", "weight": "0.5"}, {"term": "seine", "weight": "0.5"}], "min_keyword_score": "0.5"},
    "code_output": {"accepted": ["2\n3"], "normalise": {"trailing_ws": True, "blank_lines": True}, "regex": r"\d+ items"},
    "code_fill": {"blanks": {"1": {"accepted": ["range"], "normalise": {"token_ws": True}}, "2": {"accepted": ["i + 1"], "normalise": {"token_ws": True}}}},
    "code_order": {"order": ["l1", "l2", "l3", "l4"], "partial": "lcs"},
    "code_find_bug": {"bug_lines": [2, 4], "penalise_wrong": True},
}


def item(kind: str, points: str = "4") -> dict[str, Any]:
    return {"points": points, "answer_key": KEYS.get(kind)}


@pytest.mark.parametrize(
    ("kind", "response", "points"),
    [
        ("single", {"choice": "b"}, "4.00"),
        ("single", {"choice": "a"}, "0.00"),
        ("true_false", {"choice": "t"}, "4.00"),
        ("multi", {"choices": ["c", "a"]}, "4.00"),
        ("multi", {"choices": ["a"]}, "0.00"),
        ("numeric", {"value": "3.145"}, "4.00"),
        ("numeric", {"value": "3.2"}, "0.00"),
        ("numeric", {"value": "abc"}, "0.00"),
        ("short_text", {"text": "  PARIS! "}, "4.00"),
        ("code_output", {"text": "2  \n\n3\n"}, "4.00"),
        ("code_output", {"text": "12 items"}, "4.00"),
        ("code_output", {"text": "4"}, "0.00"),
        ("code_fill", {"blanks": {"1": " range ", "2": "i+1"}}, "4.00"),
        ("code_fill", {"blanks": {"1": "range", "2": "i"}}, "2.00"),
        ("code_order", {"order": ["l1", "l2", "l3", "l4"]}, "4.00"),
        ("code_order", {"order": ["l1", "l2", "l4", "l3"]}, "3.00"),
        ("code_find_bug", {"lines": [2, 4]}, "4.00"),
        ("code_find_bug", {"lines": [2, 3]}, "0.00"),
        ("code_find_bug", {"lines": [2]}, "2.00"),
    ],
)
def test_unit_table(kind: str, response: dict[str, Any], points: str) -> None:
    res = score_answer(kind, item(kind), response)
    assert res.points == D(points), (kind, response, res)
    assert res.needs_review is False


def test_multi_partial_credit() -> None:
    res = score_answer("multi", item("multi"), {"choices": ["a", "b"]}, partial_credit=True)
    assert res.points == D("0.00")  # (1 right - 1 wrong) / 2
    res = score_answer("multi", item("multi"), {"choices": ["a"]}, partial_credit=True)
    assert res.points == D("2.00")


def test_short_text_keywords_and_review_threshold() -> None:
    half = score_answer("short_text", item("short_text"), {"text": "It is the capital city"}, confidence_threshold=D("0.8"))
    assert half.points == D("2.00") and half.confidence == D("0.500") and half.needs_review is True
    full = score_answer("short_text", item("short_text"), {"text": "the capital on the seine"})
    assert full.points == D("4.00") and full.needs_review is False
    # No keywords configured: an unknown answer is sent to a human.
    plain = score_answer("short_text", {"points": "1", "answer_key": {"accepted": ["Paris"]}}, {"text": "Lutetia"})
    assert plain.points == D("0.00") and plain.needs_review is True


def test_free_form_always_reviewed() -> None:
    for kind in ("code_freeform", "essay"):
        res = score_answer(kind, {"points": "5", "rubric": {"criteria": []}}, {"text": "print('hi')"})
        assert res.needs_review is True and res.points == D(0)


def test_proficiency_map() -> None:
    assert [scoring.proficiency_for(D(p)) for p in (95, 90, 89.99, 75, 60, 59, 40, 39, 0)] == [5, 5, 4, 4, 3, 2, 2, 1, 1]


KINDS = [*KEYS, "code_freeform", "essay"]
json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-10**6, 10**6) | st.text(max_size=30) | st.floats(allow_nan=True),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=12,
)
responses = st.one_of(
    st.none(),
    st.dictionaries(st.sampled_from(["choice", "choices", "value", "text", "blanks", "order", "lines", "x"]), json_values, max_size=4),
)


@settings(max_examples=300, deadline=None)
@given(kind=st.sampled_from(KINDS), response=responses, points=st.decimals(min_value=D("0.01"), max_value=D(100), places=2))
def test_properties_bounded_deterministic_never_raises(kind: str, response: Any, points: Decimal) -> None:
    it = {"points": str(points), "answer_key": KEYS.get(kind)}
    a = score_answer(kind, it, response)
    b = score_answer(kind, it, response)
    assert a == b
    assert D(0) <= a.points <= points
    assert D(0) <= a.confidence <= D(1)


@settings(max_examples=100, deadline=None)
@given(key=st.dictionaries(st.text(max_size=10), json_values, max_size=5), response=responses)
def test_garbage_keys_never_raise(key: dict[str, Any], response: Any) -> None:
    for kind in KINDS:
        res = score_answer(kind, {"points": "1", "answer_key": key}, response)
        assert D(0) <= res.points <= D(1)


# ---------------------------------------------------------------- draw (A4, NFR-35)
POOL = [(f"v{i:02d}", 1 + i % 5) for i in range(30)]


def test_draw_deterministic_per_invitation_and_differs_across() -> None:
    inv = uuid.uuid4()
    a = draw.draw(POOL, 10, (1, 5), draw.rng_for(inv, "attempt:1"))
    b = draw.draw(POOL, 10, (1, 5), draw.rng_for(inv, "attempt:1"))
    assert a == b and len(a) == len(set(a)) == 10
    others = {tuple(draw.draw(POOL, 10, (1, 5), draw.rng_for(uuid.uuid4(), "attempt:1"))) for _ in range(5)}
    assert len(others | {tuple(a)}) > 1


def test_draw_is_difficulty_stratified() -> None:
    by_id = dict(POOL)
    picked = draw.draw(POOL, 10, (1, 5), draw.rng_for(uuid.uuid4()))
    levels = [by_id[v] for v in picked]
    assert sorted({lv: levels.count(lv) for lv in levels}.values()) == [2, 2, 2, 2, 2]
    narrow = draw.draw(POOL, 4, (2, 3), draw.rng_for(uuid.uuid4()))
    assert {by_id[v] for v in narrow} == {2, 3}
