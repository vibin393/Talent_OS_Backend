"""Semantic definition parsing and validation errors (Epic 36 story 36.2)."""

from typing import Any

import pytest

from app.core.semantic import Definition, SemanticError, validate

SRC = "candidates.candidate"


def _err(defn: Any, **kw: Any) -> SemanticError:
    with pytest.raises(SemanticError) as info:
        validate(defn, **kw)
    assert info.value.status_code == 422
    return info.value


def test_round_trip_aliases() -> None:
    raw = {
        "source": SRC,
        "filters": {"and": [{"field": "city", "op": "eq", "value": "Chennai"}, {"or": [
            {"field": "notice_period_days", "op": "lte", "value": 30},
            {"field": "notice_period_days", "op": "is_null", "value": True},
        ]}]},
        "fields": ["full_name"],
    }  # fmt: skip
    defn = validate(raw)
    assert isinstance(defn, Definition)
    assert defn.model_dump(by_alias=True, exclude_defaults=True)["filters"] == raw["filters"]


def test_unknown_field_path() -> None:
    err = _err({"source": SRC, "filters": {"field": "salary", "op": "eq", "value": 1}})
    assert err.code == "semantic_unknown_field"
    assert err.details["path"] == "filters.field"


def test_nested_path() -> None:
    err = _err({"source": SRC, "filters": {"and": [{"field": "city", "op": "eq", "value": "x"}, {"or": [
        {"field": "nope", "op": "eq", "value": 1}]}]}})  # fmt: skip
    assert err.details["path"] == "filters.and[1].or[0].field"


def test_unknown_source_and_join() -> None:
    assert _err({"source": "nope.thing"}).code == "semantic_unknown_source"
    err = _err({"source": SRC, "filters": {"field": "ghost.name", "op": "eq", "value": "x"}})
    assert err.code == "semantic_unknown_join"


def test_unknown_operator_and_disallowed_operator() -> None:
    err = _err({"source": SRC, "filters": {"field": "city", "op": "regex", "value": "x"}})
    assert err.code == "semantic_operator_not_allowed"
    assert err.details["path"] == "filters.op"
    err = _err({"source": SRC, "filters": {"field": "city", "op": "gt", "value": "x"}})
    assert err.code == "semantic_operator_not_allowed"


def test_consent_not_filterable() -> None:
    err = _err({"source": SRC, "filters": {"field": "consent_marketing", "op": "eq", "value": "granted"}})
    assert err.code == "semantic_operator_not_allowed"


@pytest.mark.parametrize(
    ("field", "op", "value"),
    [
        ("notice_period_days", "gt", "ten"),
        ("notice_period_days", "gt", True),
        ("notice_period_days", "between", [1]),
        ("experience_years", "gt", "abc"),
        ("availability_date", "lt", "2026-13-01"),
        ("availability_date", "within_days", 0),
        ("availability_date", "within_days", {"days": 5, "direction": "sideways"}),
        ("created_at", "gt", 5),
        ("id", "eq", "not-a-uuid"),
        ("id", "in", []),
        ("source", "eq", "carrier_pigeon"),
        ("willing_to_relocate", "eq", "yes"),
        ("city", "is_null", "maybe"),
        ("full_name", "contains", ""),
        ("full_name", "eq", ["a"]),
        ("skills", "skill_match", {"ref": {"kind": "global"}}),
        ("skills", "skill_match", {"ref": {"kind": "global", "id": "x"}}),
        ("skills", "skill_match", {"ref": {"kind": "planet", "id": "00000000-0000-0000-0000-000000000000"}}),
        (
            "skills",
            "skill_match",
            {"ref": {"kind": "global", "id": "00000000-0000-0000-0000-000000000000"}, "min_proficiency": 9},
        ),
    ],
)
def test_value_types(field: str, op: str, value: Any) -> None:
    err = _err({"source": SRC, "filters": {"field": field, "op": op, "value": value}})
    assert err.code == "semantic_value_type", (field, op, value, err.code)
    assert err.details["path"].startswith("filters.value")


def test_in_list_limit() -> None:
    err = _err({"source": SRC, "filters": {"field": "city", "op": "in", "value": ["x"] * 501}})
    assert err.code == "semantic_value_type"


def test_too_complex_depth_and_count() -> None:
    node: dict[str, Any] = {"field": "city", "op": "eq", "value": "x"}
    for _ in range(6):
        node = {"and": [node]}
    assert _err({"source": SRC, "filters": node}).code == "semantic_too_complex"
    many = {"or": [{"field": "city", "op": "eq", "value": str(i)} for i in range(51)]}
    assert _err({"source": SRC, "filters": many}).code == "semantic_too_complex"
    assert _err({"source": SRC, "fields": ["city"] * 51}).code == "semantic_too_complex"
    deep: dict[str, Any] = {"field": "city", "op": "eq", "value": "x"}
    for _ in range(500):
        deep = {"or": [deep]}
    assert _err({"source": SRC, "filters": deep}).code == "semantic_too_complex"


def test_extra_keys_forbidden() -> None:
    err = _err({"source": SRC, "filters": {"field": "city", "op": "eq", "value": "x", "sql": "1=1"}})
    assert err.code == "semantic_value_type"
    assert _err({"source": SRC, "raw_sql": "x"}).code == "semantic_value_type"
    assert _err({"source": SRC, "definition_version": 2}).code == "semantic_value_type"


def test_display_rules() -> None:
    assert _err({"source": SRC, "fields": ["tags.name"]}).code == "semantic_operator_not_allowed"
    assert _err({"source": SRC, "fields": ["skills"]}).code == "semantic_operator_not_allowed"
    err = _err({"source": SRC, "fields": ["city"], "group_by": [{"field": "city"}]})
    assert err.code == "semantic_operator_not_allowed"
    assert _err({"source": SRC, "group_by": [{"field": "full_name"}]}).code == "semantic_operator_not_allowed"
    err = _err({"source": SRC, "group_by": [{"field": "city", "bucket": "month"}]})
    assert err.code == "semantic_operator_not_allowed"


def test_measures_rules() -> None:
    ok = validate(
        {
            "source": SRC,
            "group_by": [{"field": "created_at", "bucket": "fy"}],
            "measures": [
                {"key": "n", "agg": "count"},
                {"key": "exp", "agg": "avg", "field": "experience_years"},
                {"key": "r", "agg": "ratio", "of": ["exp", "n"]},
                {"key": "share", "agg": "pct_of_total"},
            ],
            "sort": [{"key": "n", "desc": True}],
        }
    )
    assert len(ok.measures) == 4
    assert (
        _err({"source": SRC, "measures": [{"key": "Bad Key", "agg": "count"}]}).code == "semantic_value_type"
    )
    err = _err({"source": SRC, "measures": [{"key": "r", "agg": "ratio", "of": ["a", "b"]}]})
    assert err.code == "semantic_value_type"
    err = _err({"source": SRC, "measures": [{"key": "s", "agg": "sum", "field": "city"}]})
    assert err.code == "semantic_operator_not_allowed"
    err = _err({"source": SRC, "measures": [{"key": "s", "agg": "median", "field": "city"}]})
    assert err.code == "semantic_operator_not_allowed"
    err = _err({"source": SRC, "fields": ["city"], "sort": [{"key": "full_name"}]})
    assert err.code == "semantic_unknown_field"
