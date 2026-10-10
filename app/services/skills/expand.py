# ruff: noqa: E501
"""The only skills-graph traversal (Epic 37 A1, AD-41). ``service.expand`` and ``explain_paths`` call ``traverse``.

For an input ref R the satisfying set is: ``R parent_of C`` -> C; ``X implies R`` -> X; ``R related_to Y`` or
``Y related_to R`` -> Y (``R implies X`` does **not** add X). A depth-2 path multiplies the edge weights; a skill
reached twice keeps its best path (highest weight, then lowest depth); results below ``min_weight`` are dropped
(the input ref never is). Weights are rounded to 2 dp ``ROUND_HALF_UP`` on output. Order: input order, depth,
-weight, key.
"""

import dataclasses
from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_UP, Decimal

from app.core.errors import ValidationFailed
from app.core.skill_refs import ExpandedSkill, SkillKey
from app.services.skills.graph import Graph, Hop

MAX_DEPTH = 2
_Q = Decimal("0.01")


def q2(value: Decimal) -> Decimal:
    return value.quantize(_Q, rounding=ROUND_HALF_UP)


def check_depth(depth: int) -> None:
    if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= MAX_DEPTH:
        raise ValidationFailed(
            "depth must be between 0 and 2",
            code="depth_out_of_range",
            details={"fields": [{"field": "depth", "message": "Must be 0, 1 or 2"}]},
        )


@dataclasses.dataclass(frozen=True, slots=True)
class Reached:
    key: str
    root: str
    weight: Decimal  # exact product (rounded on output)
    depth: int
    path: tuple[Hop, ...]  # hops from the root; hop.edge explains each step

    @property
    def via(self) -> str | None:
        if not self.path:
            return None
        return self.root if len(self.path) == 1 else self.path[-2].nbr


def _better(new: Reached, cur: Reached | None) -> bool:
    if cur is None:
        return True
    return (new.weight, -new.depth) > (cur.weight, -cur.depth)


def traverse(
    graph: Graph, roots: Iterable[str], depth: int, kinds: frozenset[str], min_weight: Decimal
) -> list[Reached]:
    out: list[Reached] = []
    floor = q2(Decimal(min_weight))
    for root in dict.fromkeys(roots):
        best: dict[str, Reached] = {root: Reached(root, root, Decimal(1), 0, ())}
        frontier = [best[root]]
        for level in range(1, depth + 1):
            nxt: dict[str, Reached] = {}
            for node in sorted(frontier, key=lambda r: r.key):
                seen = {root, *(h.nbr for h in node.path)}
                for hop in graph.satisfiers.get(node.key, ()):
                    if hop.kind not in kinds or hop.nbr in seen:
                        continue
                    cand = Reached(hop.nbr, root, node.weight * hop.weight, level, (*node.path, hop))
                    if _better(cand, best.get(hop.nbr)):
                        best[hop.nbr] = cand
                        nxt[hop.nbr] = cand
            frontier = list(nxt.values())
            if not frontier:
                break
        rows = [r for k, r in best.items() if k != root and q2(r.weight) >= floor]
        rows.sort(key=lambda r: (r.depth, -q2(r.weight), r.key))
        out.append(best[root])
        out.extend(rows)
    return out


def to_expanded(rows: Sequence[Reached]) -> list[ExpandedSkill]:
    return [
        ExpandedSkill(
            key=SkillKey.parse(r.key),
            root=SkillKey.parse(r.root),
            weight=q2(r.weight),
            depth=r.depth,
            via=SkillKey.parse(r.via) if r.via is not None else None,
        )
        for r in rows
    ]
