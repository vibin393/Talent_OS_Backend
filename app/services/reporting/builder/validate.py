"""Repair check and viz validation (Epic 47 story 47.5, FR-240).

``repair_issues(definition)`` checks a stored definition against the *current* registry (no principal): a
referenced source / field / join / operator / aggregate / bucket that no longer exists yields
``{path, field, reason}`` entries. The walk lists every broken reference (the foundation stops at the first);
the foundation ``validate`` then catches anything else structural.

``check_viz(viz, definition)``: viz keys must name the definition's group_by fields, measure keys or (for a
table / pivot of rows) its fields -- the only report-specific JSON, never an expression.
"""

from collections.abc import Mapping
from typing import Any

from app.core.errors import ValidationFailed
from app.core.semantic import Definition, SemanticError, get_source, validate
from app.schemas.report_builder import VizSpec

__all__ = ["REPAIR_CODES", "check_viz", "repair_issues"]

REPAIR_CODES = frozenset(
    {
        "semantic_unknown_source",
        "semantic_unknown_field",
        "semantic_unknown_join",
        "semantic_operator_not_allowed",
    }
)


def _issue(path: str, field: str | None, reason: str) -> dict[str, Any]:
    return {"path": path, "field": field, "reason": reason}


def _walk(src: Any, node: Any, path: str, out: list[dict[str, Any]]) -> None:
    if not isinstance(node, Mapping):
        return
    for name in ("and", "or"):
        children = node.get(name)
        if isinstance(children, list):
            for i, child in enumerate(children):
                _walk(src, child, f"{path}.{name}[{i}]", out)
            return
    key = node.get("field")
    if not isinstance(key, str):
        return
    fd = src.fields.get(key)
    if fd is None:
        reason = (
            "semantic_unknown_join"
            if "." in key and key.split(".", 1)[0] not in src.joins
            else "semantic_unknown_field"
        )
        out.append(_issue(f"{path}.field", key, reason))
    elif node.get("op") not in fd.ops:
        out.append(_issue(f"{path}.op", key, "semantic_operator_not_allowed"))


def repair_issues(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Broken references of a stored definition (empty = healthy)."""
    source = definition.get("source")
    try:
        src = get_source(str(source))
    except KeyError:
        return [_issue("source", str(source), "semantic_unknown_source")]
    issues: list[dict[str, Any]] = []
    if definition.get("filters") is not None:
        _walk(src, definition["filters"], "filters", issues)
    for i, key in enumerate(definition.get("fields") or []):
        if key not in src.fields:
            issues.append(_issue(f"fields[{i}]", key, "semantic_unknown_field"))
    for i, g in enumerate(definition.get("group_by") or []):
        key = g.get("field") if isinstance(g, Mapping) else None
        fd = src.fields.get(key) if isinstance(key, str) else None
        if fd is None:
            issues.append(_issue(f"group_by[{i}].field", key, "semantic_unknown_field"))
        elif not fd.groupable or (g.get("bucket") and not fd.date_buckets):
            issues.append(_issue(f"group_by[{i}]", key, "semantic_operator_not_allowed"))
    for i, m in enumerate(definition.get("measures") or []):
        key = m.get("field") if isinstance(m, Mapping) else None
        if not isinstance(key, str):
            continue
        fd = src.fields.get(key)
        if fd is None:
            issues.append(_issue(f"measures[{i}].field", key, "semantic_unknown_field"))
            continue
        agg = m.get("agg")
        needed = "sum" if agg == "pct_of_total" else agg
        if needed not in fd.aggs:
            issues.append(_issue(f"measures[{i}].agg", key, "semantic_operator_not_allowed"))
    if issues:
        return issues
    try:
        validate(definition)
    except SemanticError as exc:
        if exc.code in REPAIR_CODES:
            return [_issue(exc.path, None, exc.code)]
        raise
    return []


def check_viz(viz: VizSpec, defn: Definition) -> VizSpec:
    """Refuse viz keys that are not part of the definition (422 ``report_viz_invalid``)."""
    groups = [g.field for g in defn.group_by]
    measures = [m.key for m in defn.measures] or (["count"] if groups else [])
    allowed_axis = set(groups)
    allowed_value = set(measures) | (set(defn.fields) if not groups and not defn.measures else set())
    for attr, allowed in (("x", allowed_axis), ("series", allowed_axis), ("value", allowed_value)):
        value = getattr(viz, attr)
        if value is not None and value not in allowed:
            raise ValidationFailed(
                f"Visualisation {attr} {value!r} is not a key of this report",
                code="report_viz_invalid",
                details={"path": f"viz.{attr}", "allowed": sorted(allowed)},
            )
    if viz.type in {"bar", "line", "pie"} and not groups:
        raise ValidationFailed(
            "Charts need at least one group-by", code="report_viz_invalid", details={"path": "viz.type"}
        )
    if viz.type == "kpi" and (groups or not defn.measures):
        raise ValidationFailed(
            "A KPI shows one measure without group-by",
            code="report_viz_invalid",
            details={"path": "viz.type"},
        )
    if viz.type == "pivot" and len(groups) < 2:
        raise ValidationFailed(
            "A pivot needs two group-bys", code="report_viz_invalid", details={"path": "viz.type"}
        )
    return viz
