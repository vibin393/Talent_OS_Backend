"""Workflow condition grammar (FR-58): validation, op x type matrix, nesting limits, pure evaluation."""

from typing import Any

import pytest

from app.core.errors import ValidationFailed
from app.services.workflow import conditions
from app.services.workflow.conditions import FieldDef
from app.services.workflow.registry import FIELD_REGISTRY, TRIGGERS, fields_for

FIELDS: dict[str, FieldDef] = {
    "s": FieldDef("str", "S"),
    "e": FieldDef("enum", "E", ("low", "high")),
    "u": FieldDef("uuid", "U"),
    "i": FieldDef("int", "I"),
    "d": FieldDef("decimal", "D"),
    "dt": FieldDef("date", "Dt"),
    "b": FieldDef("bool", "B"),
    "l": FieldDef("list", "L"),
}
UID = "5b0e8f38-6a64-4d0c-9d5d-6a0b1f7c2a11"
SNAP: dict[str, Any] = {
    "s": "Chennai Office",
    "e": "high",
    "u": UID,
    "i": 3,
    "d": "7.5",
    "dt": "2026-10-01",
    "b": True,
    "l": ["pyspark", "sql"],
}


def _one(field: str, op: str, value: Any = None) -> bool:
    clause: dict[str, Any] = {"field": field, "op": op}
    if value is not None:
        clause["value"] = value
    cond = conditions.validate({"all": [clause]}, FIELDS)
    matched, _ = conditions.evaluate(cond, SNAP, FIELDS)
    return matched


@pytest.mark.parametrize(
    ("field", "op", "value", "expected"),
    [
        ("s", "eq", "chennai office", True),
        ("s", "ne", "Pune", True),
        ("s", "contains", "office", True),
        ("s", "not_contains", "office", False),
        ("s", "starts_with", "chen", True),
        ("s", "in", ["x", "Chennai Office"], True),
        ("s", "not_in", ["Chennai Office"], False),
        ("s", "is_empty", None, False),
        ("s", "is_not_empty", None, True),
        ("e", "eq", "high", True),
        ("e", "ne", "high", False),
        ("e", "in", ["low", "high"], True),
        ("e", "not_in", ["low"], True),
        ("u", "eq", UID, True),
        ("u", "ne", UID, False),
        ("u", "in", [UID], True),
        ("i", "gt", 2, True),
        ("i", "gte", 3, True),
        ("i", "lt", 3, False),
        ("i", "lte", "3", True),
        ("i", "in", [1, 3], True),
        ("d", "gt", "7.25", True),
        ("d", "lt", 7, False),
        ("d", "eq", "7.50", True),
        ("dt", "gt", "2026-09-30", True),
        ("dt", "lte", "2026-10-01", True),
        ("dt", "eq", "2026-10-02", False),
        ("b", "eq", True, True),
        ("b", "ne", True, False),
        ("l", "contains", "PySpark", True),
        ("l", "not_contains", "java", True),
        ("l", "is_empty", None, False),
    ],
)
def test_op_type_matrix(field: str, op: str, value: Any, expected: bool) -> None:
    assert _one(field, op, value) is expected


@pytest.mark.parametrize(
    ("field", "op"),
    [
        ("e", "gt"),
        ("e", "contains"),
        ("u", "lt"),
        ("b", "gt"),
        ("b", "in"),
        ("l", "eq"),
        ("dt", "contains"),
        ("i", "starts_with"),
    ],
)
def test_op_type_mismatch_is_422(field: str, op: str) -> None:
    with pytest.raises(ValidationFailed) as exc:
        conditions.validate({"all": [{"field": field, "op": op, "value": "x"}]}, FIELDS)
    assert exc.value.code == "invalid_condition"
    assert exc.value.details["path"] == "conditions.all[0].op"


@pytest.mark.parametrize(
    ("cond", "path"),
    [
        ({"all": [{"field": "nope", "op": "eq", "value": 1}]}, "conditions.all[0].field"),
        ({"all": [{"field": "e", "op": "eq", "value": "medium"}]}, "conditions.all[0].value"),
        ({"all": [{"field": "dt", "op": "gt", "value": "abc"}]}, "conditions.all[0].value"),
        ({"all": [{"field": "i", "op": "gt", "value": True}]}, "conditions.all[0].value"),
        ({"any": [{"field": "s", "op": "in", "value": "x"}]}, "conditions.any[0].value"),
        ({"all": [{"field": "s", "op": "bogus", "value": "x"}]}, "conditions.all[0].op"),
        ({"all": []}, "conditions.all"),
        ({"xor": []}, "conditions"),
        ({"all": [{"field": "s", "op": "eq", "value": "x", "extra": 1}]}, "conditions.all[0].extra"),
    ],
)
def test_invalid_conditions_report_path(cond: dict[str, Any], path: str) -> None:
    with pytest.raises(ValidationFailed) as exc:
        conditions.validate(cond, FIELDS)
    assert exc.value.code == "invalid_condition"
    assert exc.value.details["path"] == path


def test_nesting_and_clause_limits() -> None:
    clause = {"field": "i", "op": "gt", "value": 1}
    ok = {"all": [{"any": [{"not": clause}]}]}  # depth 3
    assert conditions.validate(ok, FIELDS)
    with pytest.raises(ValidationFailed) as exc:
        conditions.validate({"all": [{"any": [{"all": [{"not": clause}]}]}]}, FIELDS)
    assert "nesting" in exc.value.details["reason"]
    assert conditions.validate({"all": [clause] * 20}, FIELDS)
    with pytest.raises(ValidationFailed) as exc:
        conditions.validate({"all": [clause] * 21}, FIELDS)
    assert "clauses" in exc.value.details["reason"]


def test_empty_condition_means_always() -> None:
    assert conditions.validate({}, FIELDS) == {}
    assert conditions.evaluate({}, {}, FIELDS) == (True, [])


def test_nested_logic_and_trace() -> None:
    cond = conditions.validate(
        {
            "any": [
                {"all": [{"field": "e", "op": "eq", "value": "low"}]},
                {"not": {"field": "b", "op": "eq", "value": False}},
            ]
        },
        FIELDS,
    )
    matched, trace = conditions.evaluate(cond, SNAP, FIELDS)
    assert matched is True
    assert [t["result"] for t in trace] == [False, False]
    assert trace[0]["actual"] == "high"


def test_incompatible_values_are_false_with_note_never_raise() -> None:
    forged = {"all": [{"field": "dt", "op": "gt", "value": "abc"}]}  # stored without validation
    matched, trace = conditions.evaluate(forged, SNAP, FIELDS)
    assert matched is False and "incompatible" in trace[0]["note"]
    matched, trace = conditions.evaluate(
        {"field": "dt", "op": "gt", "value": "2026-01-01"}, {"dt": "garbage"}, FIELDS
    )
    assert matched is False and trace[0]["note"]
    matched, trace = conditions.evaluate({"field": "ghost", "op": "eq", "value": 1}, SNAP, FIELDS)
    assert matched is False and trace[0]["note"] == "unknown field"
    matched, _ = conditions.evaluate(
        {"field": "l", "op": "contains", "value": "x"}, {"l": "not-a-list"}, FIELDS
    )
    assert matched is False
    long_value = "x" * 500
    _, trace = conditions.evaluate({"field": "s", "op": "eq", "value": "a"}, {"s": long_value}, FIELDS)
    assert len(trace[0]["actual"]) == conditions.TRACE_VALUE_MAX


def test_missing_values() -> None:
    assert conditions.evaluate({"field": "s", "op": "ne", "value": "a"}, {}, FIELDS)[0] is True
    assert conditions.evaluate({"field": "i", "op": "gt", "value": 1}, {}, FIELDS)[0] is False
    assert conditions.evaluate({"field": "s", "op": "is_empty"}, {"s": ""}, FIELDS)[0] is True


def test_registry_has_no_compensation_fields() -> None:
    from app.core.permissions import is_compensation_field

    for trigger in TRIGGERS:
        for path in fields_for(trigger):
            if path == "below_min_margin":  # a yes/no flag (spec'd), never the margin value itself
                continue
            assert not is_compensation_field(path.split(".")[-1]), path
    # Phase 2 domains add their entities through snapshot loaders (Epic 32); the Phase 1 four stay.
    assert {"demand", "application", "submission", "candidate"} <= set(FIELD_REGISTRY)
