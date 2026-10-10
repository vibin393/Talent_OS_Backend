# ruff: noqa: E501
"""Analytics Evaluation Set comparator (Epic 64 story 64.4, FR-322).

``canonical(defn)`` normalises a Definition so semantically equal definitions compare equal:

* AND / OR children are sorted (recursively) and single-child groups are unwrapped;
* ``in`` / ``not_in`` value lists are sorted and de-duplicated;
* measure key names are ignored: measures become ``(agg, field, of)`` with ``of`` mapped to the referenced measures'
  shapes; sort keys naming a measure are rewritten the same way;
* ``fields`` order is ignored;
* ``bucket=None`` equals ``day`` only for date fields declared in ``day_fields``.

``matches(actual, expected, tolerance)`` -> bool. ``tolerance`` (case-level) may relax: ``ignore_sort`` and
``ignore_fields``. Used offline (``-m evals``) and by Epic 65's live mode (``eval_threshold=0.9``).
"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["canonical", "matches"]


def _value(op: str, value: Any) -> Any:
    if op in {"in", "not_in"} and isinstance(value, list):
        return sorted({json.dumps(v, sort_keys=True) for v in value})
    return json.dumps(value, sort_keys=True)


def _node(node: Any) -> Any:
    if not isinstance(node, Mapping):
        return json.dumps(node, sort_keys=True)
    for kind in ("and", "or"):
        if kind in node:
            children = [_node(c) for c in node[kind]]
            if len(children) == 1:
                return children[0]
            # flatten nested groups of the same kind
            flat: list[Any] = []
            for c in children:
                if isinstance(c, tuple) and c and c[0] == kind:
                    flat.extend(c[1])
                else:
                    flat.append(c)
            return (kind, tuple(sorted(flat, key=repr)))
    return ("cond", node.get("field"), node.get("op"), _value(str(node.get("op")), node.get("value")))


def _measures(measures: Iterable[Mapping[str, Any]]) -> tuple[list[Any], dict[str, Any]]:
    shape: dict[str, Any] = {}
    out: list[Any] = []
    for m in measures:
        of = m.get("of")
        sig = (m.get("agg"), m.get("field"), tuple(shape.get(k, k) for k in of) if of else None)
        shape[str(m.get("key"))] = sig
        out.append(sig)
    return out, shape


def canonical(defn: Mapping[str, Any], *, day_fields: Iterable[str] = ()) -> dict[str, Any]:
    days = set(day_fields)
    measures, shape = _measures(defn.get("measures") or [])
    groups = []
    for g in defn.get("group_by") or []:
        bucket = g.get("bucket")
        if bucket == "day" and g.get("field") in days:
            bucket = None
        groups.append((g.get("field"), bucket))
    sort = [(shape.get(s.get("key"), s.get("key")), bool(s.get("desc"))) for s in defn.get("sort") or []]
    return {
        "source": defn.get("source"),
        "filters": _node(defn["filters"]) if defn.get("filters") is not None else None,
        "fields": sorted(defn.get("fields") or []),
        "group_by": groups,
        "measures": sorted(measures, key=repr),
        "sort": sort,
    }


def matches(
    actual: Mapping[str, Any], expected: Mapping[str, Any], tolerance: Mapping[str, Any] | None = None
) -> bool:
    tol = dict(tolerance or {})
    days = tol.get("day_fields") or ()
    a, e = canonical(actual, day_fields=days), canonical(expected, day_fields=days)
    if tol.get("ignore_sort"):
        a.pop("sort"), e.pop("sort")
    if tol.get("ignore_fields"):
        a.pop("fields"), e.pop("fields")
    if tol.get("ignore_group_order"):
        a["group_by"], e["group_by"] = sorted(a["group_by"], key=repr), sorted(e["group_by"], key=repr)
    return a == e
