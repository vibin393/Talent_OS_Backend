# ruff: file-ignore[line-too-long]
"""Small-cell and complementary suppression (Epic 49 A6) -- pure, no I/O.

Input: tables of cells (``applicants``, ``selected`` and, for score-based selection points, ``above`` = scoring
above the sample median) and the *constraint groups* (sets of cells whose values are tied by a published sum).

* **Primary:** a known-category cell is suppressed when ``0 < applicants < k``, ``0 < selected < k``,
  ``0 < applicants - selected < k`` (and the same two tests for ``above`` when present). Empty cells (0 applicants)
  disclose nobody and are shown.
* **Complementary:** every group must hold 0 or >= 2 suppressed cells, and for every measure (applicants, selected,
  not selected, above, not above) the suppressed cells of a group must not all be 0 while the group has a positive
  value elsewhere (otherwise their values follow from the published total). The next-smallest unsuppressed known
  cell is suppressed until a fixed point (all groups are checked together, across tables).
* **Unknown** cells are never suppressed (the unknown count is always shown); they still count in the published
  totals, so a group whose only other unsuppressed cell is the unknown cell gets another known cell suppressed.
"""

import dataclasses
from collections.abc import Iterable, Sequence

CellRef = tuple[str, str]  # (table key, category key)


@dataclasses.dataclass(slots=True)
class Cell:
    applicants: int
    selected: int
    above: int | None = None
    unknown: bool = False
    suppressed: bool = False

    def measures(self) -> tuple[int, ...]:
        base = (self.applicants, self.selected, self.applicants - self.selected)
        if self.above is None:
            return base
        return (*base, self.above, self.applicants - self.above)


def primary(cell: Cell, k: int) -> bool:
    if cell.unknown or cell.applicants == 0:
        return False
    return any(0 < v < k for v in cell.measures())


def apply(tables: dict[str, dict[str, Cell]], groups: Sequence[Iterable[CellRef]], k: int) -> set[CellRef]:
    """Mark ``suppressed`` on the cells in place and return the suppressed refs."""
    if k < 5:
        msg = "k must be at least 5"
        raise ValueError(msg)
    for cells in tables.values():
        for cell in cells.values():
            cell.suppressed = primary(cell, k)
    group_refs = [list(dict.fromkeys(g)) for g in groups]
    changed = True
    guard = 0
    while changed:
        changed = False
        guard += 1
        if guard > 10_000:  # pragma: no cover - a fixed point is always reached (cells only get suppressed)
            msg = "suppression did not converge"
            raise RuntimeError(msg)
        for refs in group_refs:
            cells = [(r, tables[r[0]][r[1]]) for r in refs]
            sup = [c for _, c in cells if c.suppressed]
            if not sup:
                continue
            free = sorted(
                ((r, c) for r, c in cells if not c.suppressed and not c.unknown),
                key=lambda rc: (rc[1].applicants, rc[0][1]),
            )
            pick: Cell | None = None
            if len(sup) == 1:
                pick = free[0][1] if free else None
            else:
                n = len(sup[0].measures())
                for i in range(n):
                    if sum(c.measures()[i] for c in sup) == 0 and any(
                        c.measures()[i] > 0 for _, c in cells if not c.suppressed
                    ):
                        pick = next((c for _, c in free if c.measures()[i] > 0), None)
                        if pick is not None:
                            break
            if pick is not None:
                pick.suppressed = True
                changed = True
    return {(t, key) for t, cells in tables.items() for key, c in cells.items() if c.suppressed}
