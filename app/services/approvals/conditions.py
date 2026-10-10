"""Approval step conditions (pure; story 4.2).

Form: ``{"all"|"any": [{"field": <whitelisted>, "op": eq|ne|gt|gte|lt|lte|in, "value": ...}]}``.

* Numeric fields compare as ``Decimal``; others compare as strings (``client_id`` as a uuid string).
* A missing or null fact makes its comparison **true**, so the step applies (conservative).
* ``validate`` raises ``ConditionError`` for an unknown field/op or a malformed value (-> 422 at chain save).
"""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

NUMERIC_FIELDS = frozenset({"bill_rate", "pay_rate", "margin_percent", "positions"})
TEXT_FIELDS = frozenset({"client_id", "country", "employment_type"})
FIELDS = NUMERIC_FIELDS | TEXT_FIELDS
ORDER_OPS = frozenset({"gt", "gte", "lt", "lte"})
OPS = frozenset({"eq", "ne", "in"}) | ORDER_OPS
COMBINATORS = ("all", "any")


class ConditionError(ValueError):
    pass


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ConditionError("boolean is not a number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConditionError(f"not a number: {value!r}") from exc
    if not result.is_finite():
        raise ConditionError(f"not a finite number: {value!r}")
    return result


def _clauses(condition: Mapping[str, Any]) -> tuple[str, list[Any]]:
    if not isinstance(condition, Mapping) or len(condition) != 1:
        raise ConditionError('a condition is {"all": [...]} or {"any": [...]}')
    ((combinator, clauses),) = condition.items()
    if combinator not in COMBINATORS:
        raise ConditionError(f"unknown combinator {combinator!r}")
    if not isinstance(clauses, list) or not clauses:
        raise ConditionError(f"{combinator!r} needs a non-empty list")
    return combinator, clauses


def validate(condition: Mapping[str, Any] | None) -> None:
    if condition is None:
        return
    _, clauses = _clauses(condition)
    for clause in clauses:
        if not isinstance(clause, Mapping) or set(clause) != {"field", "op", "value"}:
            raise ConditionError('each clause is {"field", "op", "value"}')
        field, op, value = clause["field"], clause["op"], clause["value"]
        if field not in FIELDS:
            raise ConditionError(f"unknown field {field!r}")
        if op not in OPS:
            raise ConditionError(f"unknown op {op!r}")
        if op in ORDER_OPS and field not in NUMERIC_FIELDS:
            raise ConditionError(f"op {op!r} needs a numeric field")
        values = value if op == "in" else [value]
        if op == "in" and (not isinstance(value, list) or not value):
            raise ConditionError("op 'in' needs a non-empty list")
        for v in values:
            if field in NUMERIC_FIELDS:
                _decimal(v)
            elif not isinstance(v, str) or not v:
                raise ConditionError(f"{field!r} compares with non-empty strings")


def _norm(field: str, value: Any) -> Any:
    return _decimal(value) if field in NUMERIC_FIELDS else str(value).lower()


def _clause_true(clause: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    field, op, value = clause["field"], clause["op"], clause["value"]
    fact = facts.get(field)
    if fact is None:
        return True  # missing fact -> the step applies
    try:
        left = _norm(field, fact)
    except ConditionError:
        return True
    if op == "in":
        return left in {_norm(field, v) for v in value}
    right = _norm(field, value)
    match op:
        case "eq":
            return bool(left == right)
        case "ne":
            return bool(left != right)
        case "gt":
            return bool(left > right)
        case "gte":
            return bool(left >= right)
        case "lt":
            return bool(left < right)
        case "lte":
            return bool(left <= right)
    return True  # pragma: no cover - validated ops


def evaluate(condition: Mapping[str, Any] | None, facts: Mapping[str, Any]) -> bool:
    """Return True when the step applies (no condition, condition true, or a fact is missing)."""
    if condition is None:
        return True
    combinator, clauses = _clauses(condition)
    results = [_clause_true(c, facts) for c in clauses]
    return all(results) if combinator == "all" else any(results)


def referenced_fields(condition: Mapping[str, Any] | None) -> list[str]:
    if condition is None:
        return []
    _, clauses = _clauses(condition)
    return sorted({str(c.get("field")) for c in clauses if isinstance(c, Mapping)})
