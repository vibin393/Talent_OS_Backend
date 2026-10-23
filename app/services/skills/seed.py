# ruff: file-ignore[line-too-long]
"""Platform skills graph seed (Epic 37, FR-187): ``data/skill_graph_seed.yaml``, ESCO-derived (CC BY 4.0).

``sync_graph_seed(conn)`` runs as the owner role (``talent_app`` has SELECT only on the platform tables) from
migration ``20261007_301_01_skill_graph`` and ``python -m app.cli seed-skill-graph``:

1. nodes, parent links and aliases through the P1 upsert (``candidates.skill_seed.sync_seed``) -- the file is a
   superset of the P1 seed, so P1 ids and slugs stay stable;
2. every ``skills.parent_id`` is mirrored into a ``parent_of`` edge (weight 0.80, source ``seed``);
3. the curated ``edges`` are upserted by ``(from, to, kind)`` (source ``curated``);
4. platform edges of an older ``seed_version`` are removed.

Idempotent. Emits nothing: platform changes ship with a deploy and ``seed_version`` is part of the graph cache key.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped,unused-ignore]
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.services.candidates.common import slugify
from app.services.candidates.skill_seed import load_seed, sync_seed

GRAPH_SEED_FILE = Path(__file__).resolve().parent / "data" / "skill_graph_seed.yaml"
PARENT_WEIGHT = Decimal("0.80")
EDGE_KINDS = frozenset({"parent_of", "related_to", "implies"})


@dataclass(frozen=True)
class SeedEdge:
    from_name: str
    to_name: str
    kind: str
    weight: Decimal


def _raw(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def seed_version(path: Path = GRAPH_SEED_FILE) -> str:
    return str(_raw(path).get("seed_version") or "0")


def load_edges(path: Path = GRAPH_SEED_FILE) -> list[SeedEdge]:
    out: list[SeedEdge] = []
    for e in _raw(path).get("edges") or ():
        kind = str(e["kind"])
        if kind not in EDGE_KINDS:
            msg = f"unknown edge kind {kind!r}"
            raise ValueError(msg)
        weight = Decimal(str(e["weight"])).quantize(Decimal("0.01"))
        if not Decimal(0) < weight <= Decimal(1):
            msg = f"edge weight out of range: {e!r}"
            raise ValueError(msg)
        out.append(SeedEdge(str(e["from"]).strip(), str(e["to"]).strip(), kind, weight))
    return out


def sync_graph_seed(conn: Connection, path: Path = GRAPH_SEED_FILE) -> dict[str, int]:
    """Upsert nodes, aliases, parent edges and curated edges. Returns counts. Safe to re-run."""
    if not path.exists():  # pragma: no cover - only while the seed file is being authored
        from app.services.candidates.skill_seed import SEED_FILE

        path = SEED_FILE
    version = seed_version(path)
    counts = sync_seed(conn, path)
    names = {s.name: s.slug for s in load_seed(path)}
    ids = {row[0]: row[1] for row in conn.execute(text("SELECT slug, id FROM skills"))}
    # 2. parent links -> parent_of edges
    conn.execute(
        text(
            "INSERT INTO skill_edges (id, from_skill_id, to_skill_id, kind, weight, source, seed_version) "
            "SELECT gen_random_uuid(), s.parent_id, s.id, 'parent_of', :w, 'seed', :v FROM skills s "
            "WHERE s.parent_id IS NOT NULL AND s.parent_id <> s.id "
            "ON CONFLICT (from_skill_id, to_skill_id, kind) DO UPDATE SET seed_version = EXCLUDED.seed_version, "
            "updated_at = now()"
        ),
        {"w": PARENT_WEIGHT, "v": version},
    )
    # 3. curated edges
    edges = 0
    for e in load_edges(path):
        src = ids.get(names.get(e.from_name) or slugify(e.from_name))
        dst = ids.get(names.get(e.to_name) or slugify(e.to_name))
        if src is None or dst is None or src == dst:
            msg = f"seed edge endpoint not found: {e.from_name!r} -> {e.to_name!r}"
            raise ValueError(msg)
        conn.execute(
            text(
                "INSERT INTO skill_edges (id, from_skill_id, to_skill_id, kind, weight, source, seed_version) "
                "VALUES (:id, :src, :dst, :kind, :w, 'curated', :v) "
                "ON CONFLICT (from_skill_id, to_skill_id, kind) DO UPDATE SET weight = EXCLUDED.weight, "
                "source = EXCLUDED.source, seed_version = EXCLUDED.seed_version, updated_at = now()"
            ),
            {"id": uuid.uuid4(), "src": src, "dst": dst, "kind": e.kind, "w": e.weight, "v": version},
        )
        edges += 1
    # 4. stale platform edges
    conn.execute(text("DELETE FROM skill_edges WHERE seed_version <> :v"), {"v": version})
    total = int(conn.execute(text("SELECT count(*) FROM skill_edges")).scalar_one())
    return {"skills": counts["skills"], "aliases": counts["aliases"], "edges": total, "curated": edges}
