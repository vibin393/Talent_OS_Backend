"""Workflow rule conditions (FR-58): a closed JSON grammar evaluated by plain Python -- no dynamic code
execution and no template engine anywhere in this package.

    Condition := {"all": [Condition|Clause, ...]} | {"any": [...]} | {"not": Condition|Clause}
    Clause    := {"field": "<registered path>", "op": "<op>", "value": <json>}

* ``validate(cond, fields)`` (save time) checks the grammar, the limits (depth <= 3, <= 20 clauses), that the
  field is registered, the op fits the field type and the value coerces; failures raise 422
  ``invalid_condition`` with ``details.path`` (JSON path). ``{}`` means "always".
* ``evaluate(cond, snapshot, fields)`` is pure. Coercion is explicit; incompatible values make the clause
  False with a trace note, never an exception. The trace keeps each clause's actual value (<= 100 chars).
"""

import dataclasses
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.errors import ValidationFailed

Op = Literal[
    "eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "contains", "not_contains", "is_empty", "is_not_empty",  # noqa: E501
    "starts_with",
]  # fmt: skip
FieldType = Literal["str", "int", "decimal", "date", "bool", "enum", "uuid", "list"]
OPS: tuple[str, ...] = Op.__args__  # type: ignore[attr-defined]
MAX_DEPTH = 3
MAX_CLAUSES = 20
TRACE_VALUE_MAX = 100

_EMPTY = {"is_empty", "is_not_empty"}
_ORDER = {"gt", "gte", "lt", "lte"}
OPS_BY_TYPE: dict[str, frozenset[str]] = {
    "str": frozenset({"eq", "ne", "in", "not_in", "contains", "not_contains", "starts_with", *_EMPTY}),
    "enum": frozenset({"eq", "ne", "in", "not_in", *_EMPTY}),
    "uuid": frozenset({"eq", "ne", "in", "not_in", *_EMPTY}),
    "int": frozenset({"eq", "ne", "in", "not_in", *_ORDER, *_EMPTY}),
    "decimal": frozenset({"eq", "ne", "in", "not_in", *_ORDER, *_EMPTY}),
    "date": frozenset({"eq", "ne", *_ORDER, *_EMPTY}),
    "bool": frozenset({"eq", "ne", *_EMPTY}),
    "list": frozenset({"contains", "not_contains", *_EMPTY}),
}


@dataclasses.dataclass(frozen=True, slots=True)
class FieldDef:
    type: FieldType
    label: str
    values: tuple[str, ...] = ()  # enum values


class Clause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    op: Op
    value: Any = None


# ================================================================ coercion
def coerce(ftype: str, value: Any) -> Any:
    """Explicit coercion of a JSON value to ``ftype``; raises ValueError / TypeError when impossible."""
    if value is None:
        raise ValueError("missing value")
    if ftype in ("str", "enum"):
        if not isinstance(value, str):
            raise TypeError("expected a string")
        return value
    if ftype == "list":  # list fields compare one element
        if not isinstance(value, str):
            raise TypeError("expected a string")
        return value.strip().lower()
    if ftype == "uuid":
        return str(uuid.UUID(str(value)))
    if ftype == "int":
        if isinstance(value, bool):
            raise TypeError("expected an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        raise TypeError("expected an integer")
    if ftype == "decimal":
        if isinstance(value, bool):
            raise TypeError("expected a number")
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("expected a number") from exc
    if ftype == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value.strip()[:10])
        raise TypeError("expected an ISO date")
    if ftype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise TypeError("expected true or false")
    raise ValueError(f"unknown field type {ftype}")


# ================================================================ validation
def _invalid(path: str, reason: str) -> ValidationFailed:
    return ValidationFailed(
        f"Invalid condition at {path}: {reason}",
        code="invalid_condition",
        details={"path": path, "reason": reason},
    )


def _validate_value(clause: Clause, field: FieldDef, path: str) -> Any:
    if clause.op in _EMPTY:
        return None
    ftype = field.type
    try:
        if clause.op in ("in", "not_in"):
            if not isinstance(clause.value, list) or not clause.value:
                raise ValueError("expected a non-empty list")
            values = [coerce(ftype, v) for v in clause.value]
            if ftype == "enum" and any(v not in field.values for v in values):
                raise ValueError(f"allowed values: {', '.join(field.values)}")
            return [str(v) if isinstance(v, Decimal | date) else v for v in values]
        value = coerce(
            "str" if clause.op in ("contains", "not_contains", "starts_with") and ftype == "str" else ftype,
            clause.value,
        )
        if ftype == "enum" and value not in field.values:
            raise ValueError(f"allowed values: {', '.join(field.values)}")
    except (ValueError, TypeError) as exc:
        raise _invalid(f"{path}.value", str(exc)) from exc
    return str(value) if isinstance(value, Decimal | date) else value


def validate(cond: Any, fields: Mapping[str, FieldDef], path: str = "conditions") -> dict[str, Any]:
    """Validate and normalise a condition tree against ``fields`` (see module docstring)."""
    if cond is None or cond == {}:
        return {}
    counter = [0]

    def walk(node: Any, node_path: str, depth: int) -> dict[str, Any]:
        if not isinstance(node, dict):
            raise _invalid(node_path, "expected an object")
        keys = set(node)
        if keys in ({"all"}, {"any"}):
            (key,) = keys
            if depth + 1 > MAX_DEPTH:
                raise _invalid(node_path, f"nesting deeper than {MAX_DEPTH} levels")
            items = node[key]
            if not isinstance(items, list) or not items:
                raise _invalid(f"{node_path}.{key}", "expected a non-empty list")
            return {key: [walk(item, f"{node_path}.{key}[{i}]", depth + 1) for i, item in enumerate(items)]}
        if keys == {"not"}:
            if depth + 1 > MAX_DEPTH:
                raise _invalid(node_path, f"nesting deeper than {MAX_DEPTH} levels")
            return {"not": walk(node["not"], f"{node_path}.not", depth + 1)}
        if "field" in keys:
            counter[0] += 1
            if counter[0] > MAX_CLAUSES:
                raise _invalid(node_path, f"more than {MAX_CLAUSES} clauses")
            try:
                clause = Clause.model_validate(node)
            except ValidationError as exc:
                err = exc.errors()[0]
                loc = ".".join(str(p) for p in err.get("loc", ()))
                raise _invalid(
                    f"{node_path}.{loc}" if loc else node_path, err.get("msg", "invalid clause")
                ) from exc
            field = fields.get(clause.field)
            if field is None:
                raise _invalid(f"{node_path}.field", f"unknown field '{clause.field}'")
            if clause.op not in OPS_BY_TYPE[field.type]:
                raise _invalid(
                    f"{node_path}.op", f"operator '{clause.op}' does not apply to a {field.type} field"
                )
            out: dict[str, Any] = {"field": clause.field, "op": clause.op}
            if clause.op not in _EMPTY:
                out["value"] = _validate_value(clause, field, node_path)
            return out
        raise _invalid(node_path, "expected all / any / not or a clause")

    return walk(cond, path, 0)


# ================================================================ evaluation
def _short(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    text = str(value) if not isinstance(value, list) else ", ".join(str(v) for v in value)
    return text[:TRACE_VALUE_MAX]


def _is_empty(actual: Any) -> bool:
    return actual is None or actual == "" or actual == []


def _apply(op: str, ftype: str, actual: Any, value: Any) -> bool:
    if op == "is_empty":
        return _is_empty(actual)
    if op == "is_not_empty":
        return not _is_empty(actual)
    if ftype == "list":
        items = [str(a).strip().lower() for a in (actual or [])] if isinstance(actual, list | tuple) else None
        if items is None:
            raise TypeError("the field is not a list")
        needle = coerce("list", value)
        return (needle in items) if op == "contains" else (needle not in items)
    if actual is None:
        if op in ("ne", "not_in"):
            return True
        raise ValueError("the field has no value")
    if ftype == "str":
        a = str(actual).lower()
        if op in ("in", "not_in"):
            found = a in [str(v).lower() for v in value]
            return found if op == "in" else not found
        v = str(coerce("str", value)).lower()
        return {
            "eq": a == v,
            "ne": a != v,
            "contains": v in a,
            "not_contains": v not in a,
            "starts_with": a.startswith(v),
        }[op]
    left = coerce(ftype, actual)
    if op in ("in", "not_in"):
        found = left in [coerce(ftype, v) for v in value]
        return found if op == "in" else not found
    right = coerce(ftype, value)
    if op == "eq":
        return bool(left == right)
    if op == "ne":
        return bool(left != right)
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]


def evaluate(
    cond: Mapping[str, Any] | None, snapshot: Mapping[str, Any], fields: Mapping[str, FieldDef]
) -> tuple[bool, list[dict[str, Any]]]:
    """``(matched, trace)``; pure and exception-free for any stored condition."""  # noqa: D401
    trace: list[dict[str, Any]] = []
    if not cond:
        return True, trace

    def walk(node: Any, path: str) -> bool:
        if not isinstance(node, Mapping):
            trace.append({"path": path, "result": False, "note": "invalid node"})
            return False
        if "all" in node and isinstance(node["all"], list):
            results = [walk(n, f"{path}.all[{i}]") for i, n in enumerate(node["all"])]
            return all(results)
        if "any" in node and isinstance(node["any"], list):
            results = [walk(n, f"{path}.any[{i}]") for i, n in enumerate(node["any"])]
            return any(results)
        if "not" in node:
            return not walk(node["not"], f"{path}.not")
        name, op, value = node.get("field"), node.get("op"), node.get("value")
        actual = snapshot.get(str(name)) if name is not None else None
        entry: dict[str, Any] = {
            "path": path,
            "field": name,
            "op": op,
            "value": _short(value),
            "actual": _short(actual),
        }
        field = fields.get(str(name))
        if field is None or op not in OPS:
            entry.update(result=False, note="unknown field" if field is None else "unknown operator")
        else:
            try:
                entry["result"] = bool(_apply(str(op), field.type, actual, value))
            except (ValueError, TypeError, InvalidOperation, KeyError) as exc:
                entry.update(result=False, note=f"incompatible values: {exc}"[:TRACE_VALUE_MAX])
        trace.append(entry)
        return bool(entry["result"])

    return walk(cond, "conditions"), trace
