# ruff: noqa: E501
""" "How was this calculated?" (Epic 64 story 64.3, FR-318) -- a **pure** function of the Definition, the catalogue labels
and the tenant's financial year (April - March). The model's narrative is separate and can never replace this text, so
the explanation stays correct even when the model's prose is wrong.

    describe(defn, Labels(source_label="Placements", fields={"status": "Status", ...}))
    -> "Counted placements (source: Placements) where Status is Active and Planned start date is in FY 2026-27,
        grouped by Client and Planned start date (by month). Measures: Count, Sum of Bill rate. Sorted by Count,
        descending."

Every operator, bucket and aggregate has a phrase (``OP_PHRASES`` / ``BUCKET_PHRASES`` / ``AGG_PHRASES``; pinned by
``tests/agents/analytics/test_describe.py``). Values are rendered as data (quoted strings, ISO dates, title-cased enum
values) -- never interpreted.
"""

import dataclasses
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from app.core.semantic.schema import AndNode, Condition, Definition, Measure, OrNode

__all__ = ["AGG_PHRASES", "BUCKET_PHRASES", "OP_PHRASES", "Labels", "describe", "fy_of_range"]

OP_PHRASES: dict[str, str] = {
    "eq": "is",
    "ne": "is not",
    "in": "is one of",
    "not_in": "is not one of",
    "lt": "is less than",
    "lte": "is at most",
    "gt": "is more than",
    "gte": "is at least",
    "between": "is between",
    "contains": "contains",
    "starts_with": "starts with",
    "is_null": "is empty",
    "within_days": "is within",
    "skill_match": "has the skill",
}
_DATE_OPS = {"lt": "is before", "lte": "is on or before", "gt": "is after", "gte": "is on or after"}
BUCKET_PHRASES: dict[str, str] = {
    "day": "by day",
    "week": "by week",
    "month": "by month",
    "quarter": "by quarter",
    "fy": "by financial year",
}
AGG_PHRASES: dict[str, str] = {
    "count": "Count",
    "count_distinct": "Distinct count of",
    "sum": "Sum of",
    "avg": "Average of",
    "min": "Minimum of",
    "max": "Maximum of",
    "ratio": "Ratio of",
    "pct_of_total": "Percent of total",
}
_DATE_TYPES = {"date", "datetime"}
_MAX_LIST = 10


@dataclasses.dataclass(frozen=True, slots=True)
class Labels:
    source_label: str
    fields: Mapping[str, str] = dataclasses.field(default_factory=dict)  # field key -> label
    types: Mapping[str, str] = dataclasses.field(default_factory=dict)  # field key -> FieldType
    enums: frozenset[str] = frozenset()  # enum field keys (values are title-cased)

    def label(self, key: str) -> str:
        return self.fields.get(key) or key.replace(".", " ").replace("_", " ").capitalize()


def fy_of_range(low: Any, high: Any) -> str | None:
    """``"FY 2026-27"`` when ``[low, high]`` is exactly 1 Apr .. 31 Mar of one financial year."""
    lo, hi = _as_date(low), _as_date(high)
    if lo is None or hi is None:
        return None
    if (lo.month, lo.day) == (4, 1) and (hi.year, hi.month, hi.day) == (lo.year + 1, 3, 31):
        return f"FY {lo.year}-{(lo.year + 1) % 100:02d}"
    return None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _value(value: Any, key: str, labels: Labels) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None:
        return "empty"
    if key in labels.enums and isinstance(value, str):
        return value.replace("_", " ").capitalize()
    if isinstance(value, int | float):
        return f"{value:,}" if isinstance(value, int) else str(value)
    text = str(value)
    if labels.types.get(key) in _DATE_TYPES:
        return text[:10] if labels.types.get(key) == "date" else text
    return f'"{text}"'


def _values(values: Any, key: str, labels: Labels) -> str:
    items = list(values) if isinstance(values, list | tuple) else [values]
    shown = [_value(v, key, labels) for v in items[:_MAX_LIST]]
    more = f" and {len(items) - _MAX_LIST} more" if len(items) > _MAX_LIST else ""
    if len(shown) <= 1:
        return (shown[0] if shown else "nothing") + more
    return ", ".join(shown[:-1]) + (" or " if not more else ", ") + shown[-1] + more


def _condition(c: Condition, labels: Labels) -> str:
    name = labels.label(c.field)
    is_date = labels.types.get(c.field) in _DATE_TYPES
    op = c.op
    if op == "is_null":
        return f"{name} {'is empty' if c.value is not False else 'is not empty'}"
    if op == "between" and isinstance(c.value, list | tuple) and len(c.value) == 2:
        fy = fy_of_range(c.value[0], c.value[1]) if is_date else None
        if fy:
            return f"{name} is in {fy}"
        return f"{name} is between {_value(c.value[0], c.field, labels)} and {_value(c.value[1], c.field, labels)}"
    if op == "within_days":
        days, direction = c.value, "past"
        if isinstance(c.value, dict):
            days, direction = c.value.get("days"), c.value.get("direction", "past")
        return f"{name} is within the {'next' if direction == 'future' else 'last'} {days} days"
    if op == "skill_match":
        return f"{name} matches a selected skill"
    if op in {"in", "not_in"}:
        return f"{name} {OP_PHRASES[op]} {_values(c.value, c.field, labels)}"
    phrase = _DATE_OPS.get(op, OP_PHRASES[op]) if is_date else OP_PHRASES[op]
    return f"{name} {phrase} {_value(c.value, c.field, labels)}"


def _node(node: Any, labels: Labels, top: bool = False) -> str:
    if isinstance(node, Condition):
        return _condition(node, labels)
    if isinstance(node, AndNode):
        parts = [_node(n, labels) for n in node.and_]
        text = " and ".join(parts)
        return text if top or len(parts) == 1 else f"({text})"
    assert isinstance(node, OrNode)
    parts = [_node(n, labels) for n in node.or_]
    text = " or ".join(parts)
    return text if len(parts) == 1 else f"({text})"


def measure_label(m: Measure, labels: Labels, keyed: Mapping[str, str] | None = None) -> str:
    keyed = keyed or {}
    if m.agg == "ratio" and m.of is not None:
        return f"Ratio of {keyed.get(m.of[0], m.of[0])} to {keyed.get(m.of[1], m.of[1])}"
    if m.agg == "pct_of_total":
        return "Percent of total" + (f" {labels.label(m.field)}" if m.field else " rows")
    if m.field is None:
        return "Count"
    if m.agg == "count":
        return f"Count of {labels.label(m.field)}"
    return f"{AGG_PHRASES[m.agg]} {labels.label(m.field)}"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def describe(defn: Definition, labels: Labels) -> str:
    """Plain-English explanation of ``defn`` (deterministic)."""
    source = labels.source_label
    noun = source.lower()
    aggregate = bool(defn.group_by or defn.measures)
    keyed: dict[str, str] = {}
    for m in defn.measures:
        keyed[m.key] = measure_label(m, labels, keyed)
    has_count = not defn.measures or any(m.agg == "count" and m.field is None for m in defn.measures)
    if aggregate:
        verb = "Counted" if has_count else "Summarised"
    else:
        verb = "Listed"
    text = f"{verb} {noun} (source: {source})"
    if defn.filters is not None:
        text += " where " + _node(defn.filters, labels, top=True)
    if defn.group_by:
        groups = [
            labels.label(g.field) + (f" ({BUCKET_PHRASES[g.bucket]})" if g.bucket else "")
            for g in defn.group_by
        ]
        text += f", grouped by {_join(groups)}"
    text += "."
    if aggregate:
        measures = list(keyed.values()) or ["Count"]
        text += f" Measures: {', '.join(measures)}."
    elif defn.fields:
        text += f" Showing {', '.join(labels.label(k) for k in defn.fields)}."
    if defn.sort:
        sorts = [
            f"{keyed.get(s.key) or labels.label(s.key)}, {'descending' if s.desc else 'ascending'}"
            for s in defn.sort
        ]
        text += f" Sorted by {'; then '.join(sorts)}."
    return text
