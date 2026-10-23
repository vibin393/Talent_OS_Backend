# ruff: file-ignore[line-too-long]
"""Minimum cell size (Epic 64 story 64.2, FR-317). Pure.

``MIN_CELL_SIZE`` maps a semantic source key to ``k``: for a **grouped** answer over a listed source, every group whose
record count is below ``k`` is replaced by ``{"suppressed": True}`` and counted in ``suppressed_cells``; the UI notes
"Groups with fewer than k records are hidden." The record count is the definition's plain ``count`` measure (or the
implicit ``count`` column when the definition has no measures). A grouped answer over a listed source **without** a
count is fail-closed: every group is suppressed.

The map is empty by default (no P1-P3 source declares a minimum cell size today -- Known gap 1); source owners add
entries additively (``register_min_cell_size``).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.core.semantic.schema import Definition

__all__ = ["MIN_CELL_SIZE", "SUPPRESSED", "min_cell_size", "register_min_cell_size", "suppress"]

MIN_CELL_SIZE: dict[str, int] = {}
SUPPRESSED: dict[str, Any] = {"suppressed": True}


def register_min_cell_size(source_key: str, k: int) -> None:
    if k < 2:
        msg = "a minimum cell size is at least 2"
        raise ValueError(msg)
    MIN_CELL_SIZE[source_key] = k


def min_cell_size(source_key: str) -> int | None:
    return MIN_CELL_SIZE.get(source_key)


def _count_key(defn: Definition) -> str | None:
    if not defn.measures:
        return "count"
    for m in defn.measures:
        if m.agg == "count" and m.field is None:
            return m.key
    return None


def suppress(
    source_key: str, defn: Definition, rows: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """``(rows with small groups replaced by SUPPRESSED, suppressed count)``."""
    k = MIN_CELL_SIZE.get(source_key)
    if k is None or not defn.group_by:
        return [dict(r) for r in rows], 0
    key = _count_key(defn)
    out: list[dict[str, Any]] = []
    hidden = 0
    for row in rows:
        value = row.get(key) if key is not None else None
        try:
            small = key is None or value is None or int(value) < k
        except (TypeError, ValueError):
            small = True
        if small:
            out.append(dict(SUPPRESSED))
            hidden += 1
        else:
            out.append(dict(row))
    return out, hidden
