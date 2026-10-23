# ruff: file-ignore[line-too-long]
"""Adapt a saved definition to one viewer's catalogue (Epic 47 story 47.2/47.3; pure, no I/O).

``viewer_definition(defn, viz, catalogue)``:

* a **measure** whose ``field`` is masked for the viewer (``compensation`` / ``identity`` without the view
  permission) is removed, together with every ``ratio`` that depends on a removed measure and every sort entry
  naming a removed key -- they are reported as masked, so the run does not fail;
* masked entries in ``fields`` stay: the foundation returns them as ``null`` and lists them in ``masked_fields``;
* a masked field in **filters or group_by** is NOT removed: the foundation refuses it with 422
  ``semantic_field_forbidden`` for this viewer (a run never silently drops a filter).

``viz`` references to removed measures fall back to the first remaining measure (or ``count``).
"""

import dataclasses
from collections.abc import Iterable

from app.core.semantic import Definition, Measure
from app.schemas.report_builder import VizSpec
from app.schemas.semantic import SourceCatalogOut

__all__ = ["Adapted", "masked_keys", "viewer_definition"]


@dataclasses.dataclass(frozen=True, slots=True)
class Adapted:
    definition: Definition
    viz: VizSpec
    removed_measures: tuple[str, ...]
    masked: tuple[str, ...]  # removed measure keys + the masked fields they were over


def masked_keys(catalogue: SourceCatalogOut | None) -> frozenset[str]:
    if catalogue is None:
        return frozenset()
    return frozenset(f.key for f in catalogue.fields if f.masked)


def _cascade(defn: Definition, masked_fields: Iterable[str]) -> tuple[list[str], set[str]]:
    masked = set(masked_fields)
    removed: list[str] = []
    over: set[str] = set()
    gone: set[str] = set()
    for m in defn.measures:
        if m.field is not None and m.field in masked:
            removed.append(m.key)
            gone.add(m.key)
            over.add(m.field)
        elif m.agg == "ratio" and m.of is not None and set(m.of) & gone:
            removed.append(m.key)
            gone.add(m.key)
    return removed, over


def viewer_definition(defn: Definition, viz: VizSpec, catalogue: SourceCatalogOut | None) -> Adapted:
    removed, over = _cascade(defn, masked_keys(catalogue))
    if not removed:
        return Adapted(defn, viz, (), ())
    gone = set(removed)
    measures = [m for m in defn.measures if m.key not in gone]
    if not measures and not defn.group_by:
        # A KPI over masked fields only: keep it an aggregate (a plain count), never a row listing.
        measures = [Measure(key="count", agg="count")]
    sort = [s for s in defn.sort if s.key not in gone]
    adapted = defn.model_copy(update={"measures": measures, "sort": sort})
    fallback = measures[0].key if measures else "count"
    new_viz = viz
    if viz.value in gone:
        new_viz = viz.model_copy(update={"value": fallback})
    return Adapted(adapted, new_viz, tuple(removed), tuple(sorted(gone | over)))
