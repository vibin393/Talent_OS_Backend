# ruff: file-ignore[line-too-long, ambiguous-unicode-character-string]
"""Adverse-impact tables (Epic 49 A5, FR-247) -- pure, no I/O.

``build_snapshot(records, dimensions, intersections, k, score_based)``:

* ``records``: one ``Record`` per candidate -- ``categories`` (``{dimension: option | None}``; ``None`` = unknown),
  ``selected`` and ``score`` (score-based selection points). No ids enter this module.
* One table per enabled dimension (known options + the always-shown ``unknown`` row) and one per intersection
  (known x known cells + one ``unknown`` row for candidates unknown in either dimension).
* Per row: applicants, selected, selection rate, impact ratio (rate / highest *shown* known rate) and the
  four-fifths flag (ratio < 0.8); score-based points add the LL144 scoring rate (share scoring above the sample
  median) with its own impact ratio and flag.
* ``suppression.apply`` hides cells under k plus complementary cells (groups: each table total; each intersection row
  and column). Suppressed values render as ``"<k"``; their rates / ratios / flags are null.

The snapshot carries counts and category keys only -- never candidate ids or per-person rows.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from statistics import median
from typing import Any

from app.services.fairness import suppression
from app.services.fairness.suppression import Cell

UNKNOWN = "unknown"
FOUR_FIFTHS = Decimal("0.8")
Q = Decimal("0.0001")


@dataclasses.dataclass(frozen=True, slots=True)
class Record:
    categories: Mapping[str, str | None]
    selected: bool
    score: Decimal | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Dimension:
    key: str
    label: str
    options: Sequence[tuple[str, str]]  # known options (no prefer_not_to_say)


def _exact(num: int, den: int) -> Decimal | None:
    return Decimal(num) / Decimal(den) if den else None


def _rate(num: int, den: int) -> Decimal | None:
    return (Decimal(num) / Decimal(den)).quantize(Q, ROUND_HALF_UP) if den else None


def _s(d: Decimal | None) -> str | None:
    return None if d is None else format(d, "f")


def _cells(
    records: Sequence[Record], keyfn: Any, keys: Sequence[str], med: Decimal | None
) -> dict[str, Cell]:
    cells = {key: Cell(0, 0, 0 if med is not None else None, unknown=(key == UNKNOWN)) for key in keys}
    for r in records:
        key = keyfn(r)
        cell = cells[key]
        cell.applicants += 1
        cell.selected += 1 if r.selected else 0
        if med is not None and r.score is not None and r.score > med:
            cell.above = (cell.above or 0) + 1
    return cells


def build_snapshot(
    records: Sequence[Record],
    dimensions: Sequence[Dimension],
    intersections: Sequence[tuple[str, str]],
    k: int,
    *,
    score_based: bool,
    category_set_version: int,
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    scores = [r.score for r in records if r.score is not None]
    med = Decimal(str(median(scores))) if score_based and scores else None
    by_key = {d.key: d for d in dimensions}
    tables: dict[str, dict[str, Cell]] = {}
    labels: dict[str, dict[str, str]] = {}
    meta: dict[str, list[str]] = {}
    groups: list[list[tuple[str, str]]] = []

    def cat(r: Record, dim: str) -> str:
        value = r.categories.get(dim)
        return value if value and value in labels_of[dim] else UNKNOWN

    labels_of = {d.key: dict(d.options) for d in dimensions}
    for d in dimensions:
        keys = [o for o, _ in d.options] + [UNKNOWN]
        tables[d.key] = _cells(records, lambda r, _d=d.key: cat(r, _d), keys, med)
        labels[d.key] = {**dict(d.options), UNKNOWN: "Unknown / not disclosed"}
        meta[d.key] = [d.key]
        groups.append([(d.key, key) for key in keys])
    for a, b in intersections:
        if a not in by_key or b not in by_key:
            continue
        tkey = f"{a}×{b}"
        known = [(x, y) for x, _ in by_key[a].options for y, _ in by_key[b].options]
        keys = [f"{x}|{y}" for x, y in known] + [UNKNOWN]

        def ikey(r: Record, _a: str = a, _b: str = b) -> str:
            x, y = cat(r, _a), cat(r, _b)
            return UNKNOWN if UNKNOWN in (x, y) else f"{x}|{y}"

        tables[tkey] = _cells(records, ikey, keys, med)
        labels[tkey] = {
            **{f"{x}|{y}": f"{labels_of[a][x]} / {labels_of[b][y]}" for x, y in known},
            UNKNOWN: "Unknown in either category",
        }
        meta[tkey] = [a, b]
        groups.append([(tkey, key) for key in keys])
        for x, _ in by_key[a].options:
            groups.append([(tkey, f"{x}|{y}") for y, _ in by_key[b].options])
        for y, _ in by_key[b].options:
            groups.append([(tkey, f"{x}|{y}") for x, _ in by_key[a].options])
    suppression.apply(tables, groups, k)
    hidden = f"<{k}"
    out_tables: list[dict[str, Any]] = []
    unknown_counts: dict[str, int] = {}
    for tkey, cells in tables.items():
        shown_known = [c for c in cells.values() if not c.suppressed and not c.unknown and c.applicants]
        best = max((_exact(c.selected, c.applicants) or Decimal(0) for c in shown_known), default=None)
        best_s = (
            max((_exact(c.above or 0, c.applicants) or Decimal(0) for c in shown_known), default=None)
            if med is not None
            else None
        )
        rows: list[dict[str, Any]] = []
        for key, c in cells.items():
            row: dict[str, Any] = {
                "category": key, "label": labels[tkey].get(key, key), "unknown": c.unknown, "suppressed": c.suppressed,
            }  # fmt: skip
            if c.suppressed:
                row.update(applicants=hidden, selected=hidden, rate=None, impact_ratio=None, flag=None)
                if med is not None:
                    row.update(scoring_rate=None, scoring_impact_ratio=None, scoring_flag=None)
            else:
                rate = _rate(c.selected, c.applicants)
                ratio = (
                    (_exact(c.selected, c.applicants) / best).quantize(Q, ROUND_HALF_UP)  # type: ignore[operator]
                    if rate is not None and best and not c.unknown
                    else None
                )
                row.update(
                    applicants=c.applicants,
                    selected=c.selected,
                    rate=_s(rate),
                    impact_ratio=_s(ratio),
                    flag=(ratio < FOUR_FIFTHS) if ratio is not None else None,
                )
                if med is not None:
                    srate = _rate(c.above or 0, c.applicants)
                    sratio = (
                        (_exact(c.above or 0, c.applicants) / best_s).quantize(Q, ROUND_HALF_UP)  # type: ignore[operator]
                        if srate is not None and best_s and not c.unknown
                        else None
                    )
                    row.update(
                        scoring_rate=_s(srate),
                        scoring_impact_ratio=_s(sratio),
                        scoring_flag=(sratio < FOUR_FIFTHS) if sratio is not None else None,
                    )
            rows.append(row)
        unknown_counts[tkey] = cells[UNKNOWN].applicants
        out_tables.append(
            {
                "key": tkey,
                "dimensions": meta[tkey],
                "rows": rows,
                "totals": {
                    "applicants": sum(c.applicants for c in cells.values()),
                    "selected": sum(c.selected for c in cells.values()),
                },
            }
        )
    return {
        "category_set_version": category_set_version,
        "k": k,
        "suppressed_label": hidden,
        "method": "both" if med is not None else "selection_rate",
        "median_score": _s(med.quantize(Decimal("0.01"))) if med is not None else None,
        "tables": out_tables,
        "notes": list(notes),
        "unknown_counts": unknown_counts,
        "total_applicants": len(records),
    }
