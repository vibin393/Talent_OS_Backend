# ruff: noqa: E501
"""Segment definitions on the semantic layer (spec A3, AD-42, NFR-33).

A Segment stores a ``core.semantic`` ``Definition`` restricted to ``{definition_version, source, filters}``:
membership needs subject ids only, so ``fields`` / ``group_by`` / ``measures`` / ``sort`` are refused. Every
check is the foundation's (``validate(defn, principal=<author>)``); this module only shapes the JSON, derives
the stored member *reason* and lists the skill leaves (for expansion reasons and graph-change refreshes).
"""

import uuid
from collections.abc import Iterator, Mapping
from typing import Any

from app.core.errors import ValidationFailed
from app.core.permissions import Principal
from app.core.semantic import AndNode, Condition, Definition, OrNode, get_source, validate
from app.models.talent_pools import SEGMENT_SOURCES

__all__ = [
    "PREVIEW_FIELDS",
    "build",
    "conditions",
    "expanding_skill_refs",
    "matched_labels",
    "stored",
    "uuid_values",
]

# Columns shown by the builder preview (masked by the foundation when the viewer may not see them).
PREVIEW_FIELDS: dict[str, tuple[str, ...]] = {
    "candidates.candidate": ("full_name", "current_title", "city", "notice_period_days"),
    "placements.worker": ("candidate.full_name", "role_title", "status", "effective_end_date"),
}

_OP_LABELS = {
    "eq": "is",
    "ne": "is not",
    "in": "is one of",
    "not_in": "is not one of",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
    "between": "between",
    "contains": "contains",
    "starts_with": "starts with",
    "is_null": "is empty",
    "within_days": "within",
    "skill_match": "has skill",
}


def _bad(message: str, path: str, code: str = "segment_definition_invalid") -> ValidationFailed:
    return ValidationFailed(message, code=code, details={"path": path})


def build(source_key: str, raw: Mapping[str, Any] | None, *, principal: Principal | None) -> Definition:
    """Shape + validate a segment definition (author's field permissions apply when ``principal`` is given)."""
    if source_key not in SEGMENT_SOURCES:
        raise _bad(f"Segments use {', '.join(SEGMENT_SOURCES)}", "source_key", "semantic_unknown_source")
    body = dict(raw or {})
    if body.get("source") not in (None, source_key):
        raise _bad("definition.source must equal source_key", "source")
    extra = set(body) - {"filters", "source", "definition_version"}
    if extra:
        raise _bad("A segment definition has filters only", sorted(extra)[0])
    if body.get("filters") is None:
        raise _bad("A segment needs at least one condition", "filters")
    return validate(
        {"definition_version": 1, "source": source_key, "filters": body["filters"]}, principal=principal
    )


def stored(defn: Definition) -> dict[str, Any]:
    """JSON stored in ``segments.definition`` / ``segment_versions.definition``."""
    return defn.model_dump(mode="json", by_alias=True, include={"definition_version", "source", "filters"})


def conditions(node: Any) -> Iterator[Condition]:
    if isinstance(node, Condition):
        yield node
    elif isinstance(node, AndNode):
        for child in node.and_:
            yield from conditions(child)
    elif isinstance(node, OrNode):
        for child in node.or_:
            yield from conditions(child)


def expanding_skill_refs(defn: Definition) -> list[str]:
    """``"global:<uuid>"`` keys of ``skill_match`` leaves with ``expand=true`` (order kept, deduplicated)."""
    out: list[str] = []
    for c in conditions(defn.filters):
        if c.op == "skill_match" and isinstance(c.value, dict) and c.value.get("expand") is True:
            ref = c.value.get("ref") or {}
            key = f"{ref.get('kind')}:{ref.get('id')}"
            if key not in out:
                out.append(key)
    return out


def uuid_values(defn: Definition) -> set[uuid.UUID]:
    """Every uuid-looking value of the definition (checked against candidate ids: no person data, A10)."""
    out: set[uuid.UUID] = set()
    for c in conditions(defn.filters):
        if c.op == "skill_match":
            continue
        values = c.value if isinstance(c.value, list) else [c.value]
        for v in values:
            try:
                out.add(uuid.UUID(str(v)))
            except (TypeError, ValueError, AttributeError):
                continue
    return out


def _value_text(value: Any) -> str:
    if isinstance(value, dict):
        if "days" in value:
            return f"{value.get('days')} days ({value.get('direction', 'past')})"
        if "min_proficiency" in value and value.get("min_proficiency"):
            return f"level {value['min_proficiency']}+" + (" incl. related" if value.get("expand") else "")
        return "related skills" if value.get("expand") else ""
    if isinstance(value, list):
        text = ", ".join(str(v) for v in value[:5])
        return text + (" ..." if len(value) > 5 else "")
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)[:60]


def _label(source_key: str, c: Condition) -> dict[str, str]:
    try:
        field_label = get_source(source_key).fields[c.field].label
    except KeyError:
        field_label = c.field
    op = c.op
    if op == "is_null":
        text = f"{field_label} {'is empty' if c.value else 'is set'}"
    else:
        text = f"{field_label} {_OP_LABELS.get(op, op)} {_value_text(c.value)}".strip()
    return {"field": c.field, "op": op, "label": text[:200]}


def _group_label(source_key: str, node: Any) -> dict[str, str]:
    if isinstance(node, Condition):
        return _label(source_key, node)
    kind = "all of" if isinstance(node, AndNode) else "any of"
    inner = [_label(source_key, c)["label"] for c in conditions(node)][:4]
    return {
        "field": "",
        "op": "and" if isinstance(node, AndNode) else "or",
        "label": f"{kind}: {'; '.join(inner)}"[:200],
    }


def matched_labels(defn: Definition) -> list[dict[str, str]]:
    """Reason labels: the AND-level conditions (nested groups summarised); an OR root lists its groups."""
    root = defn.filters
    if root is None:
        return []
    if isinstance(root, Condition):
        return [_label(defn.source, root)]
    children = root.and_ if isinstance(root, AndNode) else root.or_
    return [_group_label(defn.source, child) for child in children][:20]
