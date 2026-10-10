# ruff: noqa: E501
"""Effective skills graph of a tenant (Epic 37 A1, FR-187, AD-41): platform edges + tenant edges + legacy tenant
parents (``tenant_skills.parent_skill_id`` read as an implicit tenant ``parent_of`` edge at 0.80).

* ``graph_version(db)`` -- a stable hash of everything the effective graph depends on (platform ``seed_version`` and
  edge stamp, the tenant edges, legacy parents, settings). It keys the cache and enters match input hashes.
* ``load(db, tenant_id)`` -- the ``Graph`` (satisfier index used by ``expand.py``), cached per tenant and version.
* ``assert_no_parent_cycle`` -- recursive CTE over every ``parent_of`` source, inside the write transaction.
* tenant edge CRUD, node view and labels.

Only ``expand.py`` walks the graph (AD-41); this module loads and validates it.
"""

import dataclasses
import hashlib
import threading
import uuid
from collections import OrderedDict
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.skill_refs import SkillKey
from app.events.skills import SkillGraphChanged
from app.models.candidates import Skill, SkillAlias, TenantSkill, TenantSkillAlias
from app.models.skills_graph import SkillEdge, TenantSkillEdge
from app.schemas.skills import SkillEdgeIn, SkillEdgeOut, SkillEdgeUpdate, SkillLabel, SkillNodeOut
from app.services.skills import settings as graph_settings

LEGACY_PARENT_WEIGHT = Decimal("0.80")
CYCLE_DEPTH = 20
CACHE_SIZE = 64


@dataclasses.dataclass(frozen=True, slots=True)
class Edge:
    src: str  # SkillKey.as_str()
    dst: str
    kind: str
    weight: Decimal
    origin: str  # platform | tenant | legacy
    edge_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class Hop:
    """One satisfying step: ``nbr`` satisfies the node it hangs off, through ``edge``."""

    nbr: str
    weight: Decimal
    kind: str
    edge: Edge


@dataclasses.dataclass(frozen=True)
class Graph:
    version: str
    satisfiers: dict[str, tuple[Hop, ...]]
    edges: tuple[Edge, ...]


# ================================================================ keys and labels
def gkey(skill_id: uuid.UUID | str) -> str:
    return f"global:{skill_id}"


def tkey(tenant_skill_id: uuid.UUID | str) -> str:
    return f"tenant:{tenant_skill_id}"


def ref_key(global_id: uuid.UUID | None, tenant_id: uuid.UUID | None) -> str:
    return gkey(global_id) if global_id is not None else tkey(tenant_id)  # type: ignore[arg-type]


def parse_key(raw: str, field: str = "key") -> SkillKey:
    try:
        return SkillKey.parse(raw)
    except ValueError:
        raise ValidationFailed(
            "Invalid skill key",
            code="invalid_skill_key",
            details={"fields": [{"field": field, "message": "Expected global:<uuid> or tenant:<uuid>"}]},
        ) from None


async def labels(db: AsyncSession, keys: set[str]) -> dict[str, SkillLabel]:
    gids = {SkillKey.parse(k).id for k in keys if k.startswith("global:")}
    tids = {SkillKey.parse(k).id for k in keys if k.startswith("tenant:")}
    out: dict[str, SkillLabel] = {}
    if gids:
        for s in (await db.execute(select(Skill).where(Skill.id.in_(gids)))).scalars():
            out[gkey(s.id)] = SkillLabel(key=gkey(s.id), name=s.name, category=s.category, status="approved")
    if tids:
        for t in (await db.execute(select(TenantSkill).where(TenantSkill.id.in_(tids)))).scalars():
            out[tkey(t.id)] = SkillLabel(key=tkey(t.id), name=t.name, category=t.category, status=t.status)
    for k in keys - set(out):
        out[k] = SkillLabel(key=k, name=k, status="unknown")
    return out


async def keys_for_ids(db: AsyncSession, ids: list[uuid.UUID]) -> list[SkillKey]:
    """``SkillKey`` of plain skill ids (search filters carry ids only): global when the id is in ``skills``."""
    if not ids:
        return []
    found = set((await db.execute(select(Skill.id).where(Skill.id.in_(ids)))).scalars())
    return [SkillKey(kind="global" if i in found else "tenant", id=i) for i in ids]


async def _label_of(db: AsyncSession, key: SkillKey) -> SkillLabel | None:
    if key.kind == "global":
        s = await db.get(Skill, key.id)
        if s is None or not s.is_active:
            return None
        return SkillLabel(key=key.as_str(), name=s.name, category=s.category, status="approved")
    t = await db.get(TenantSkill, key.id)
    if t is None:
        return None
    return SkillLabel(key=key.as_str(), name=t.name, category=t.category, status=t.status)


# ================================================================ version and loading
async def graph_version(db: AsyncSession) -> str:
    """Stable hash of the tenant's effective graph (changes whenever an expansion result could change)."""
    platform = (
        await db.execute(
            select(func.count(SkillEdge.id), func.max(SkillEdge.updated_at), func.max(SkillEdge.seed_version))
        )
    ).one()
    tenant = (
        await db.execute(
            text(
                "SELECT md5(coalesce(string_agg(id::text || ':' || weight::text || ':' || "
                "extract(epoch FROM updated_at)::text, ',' ORDER BY id), '')) FROM tenant_skill_edges"
            )
        )
    ).scalar_one()
    legacy = (
        await db.execute(
            text(
                "SELECT md5(coalesce(string_agg(id::text || '>' || parent_skill_id::text || ':' || status, ',' "
                "ORDER BY id), '')) FROM tenant_skills WHERE parent_skill_id IS NOT NULL"
            )
        )
    ).scalar_one()
    cfg = await graph_settings.current(db)
    raw = f"{platform[2]}|{platform[0]}|{platform[1]}|{tenant}|{legacy}|{cfg.fingerprint()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def _edges(db: AsyncSession) -> list[Edge]:
    out: list[Edge] = []
    rows = await db.execute(
        select(SkillEdge.id, SkillEdge.from_skill_id, SkillEdge.to_skill_id, SkillEdge.kind, SkillEdge.weight)
    )
    out += [Edge(gkey(r[1]), gkey(r[2]), r[3], Decimal(r[4]), "platform", r[0]) for r in rows]
    for e in (await db.execute(select(TenantSkillEdge))).scalars():
        out.append(
            Edge(
                ref_key(e.from_skill_id, e.from_tenant_skill_id),
                ref_key(e.to_skill_id, e.to_tenant_skill_id),
                e.kind,
                Decimal(e.weight),
                "tenant",
                e.id,
            )
        )
    legacy = await db.execute(
        select(TenantSkill.id, TenantSkill.parent_skill_id).where(
            TenantSkill.parent_skill_id.is_not(None), TenantSkill.status != "rejected"
        )
    )
    out += [Edge(gkey(r[1]), tkey(r[0]), "parent_of", LEGACY_PARENT_WEIGHT, "legacy", None) for r in legacy]
    return out


def build(version: str, edges: list[Edge]) -> Graph:
    """Satisfier index: ``R parent_of C`` -> C satisfies R; ``X implies R`` -> X satisfies R; ``related_to``
    both ways. Parallel edges keep the highest weight (platform before tenant on ties)."""
    best: dict[str, dict[tuple[str, str], Hop]] = {}

    def add(node: str, hop: Hop) -> None:
        slot = best.setdefault(node, {})
        cur = slot.get((hop.nbr, hop.kind))
        if cur is None or hop.weight > cur.weight:
            slot[hop.nbr, hop.kind] = hop

    for e in edges:
        if e.src == e.dst:
            continue
        if e.kind == "parent_of":
            add(e.src, Hop(e.dst, e.weight, e.kind, e))
        elif e.kind == "implies":
            add(e.dst, Hop(e.src, e.weight, e.kind, e))
        elif e.kind == "related_to":
            add(e.src, Hop(e.dst, e.weight, e.kind, e))
            add(e.dst, Hop(e.src, e.weight, e.kind, e))
    satisfiers = {
        node: tuple(sorted(hops.values(), key=lambda h: (-h.weight, h.nbr, h.kind)))
        for node, hops in best.items()
    }
    return Graph(version=version, satisfiers=satisfiers, edges=tuple(edges))


def implied_by(graph: Graph, key: str) -> list[Edge]:
    """Direct ``key implies Y`` edges (a lookup, not a traversal): what a skill implies, for graph suggestions."""
    return sorted(
        (e for e in graph.edges if e.kind == "implies" and e.src == key and e.dst != key),
        key=lambda e: (-e.weight, e.dst),
    )


_cache: "OrderedDict[str, Graph]" = OrderedDict()
_lock = threading.Lock()


def cache_key(tenant_id: uuid.UUID | None, version: str) -> str:
    return f"skills:graph:{tenant_id}:{version}"


def invalidate(tenant_id: uuid.UUID | None = None) -> int:
    """Drop the cached graphs of ``tenant_id`` (all tenants when None). Returns the number dropped."""
    prefix = f"skills:graph:{tenant_id}:" if tenant_id is not None else "skills:graph:"
    with _lock:
        stale = [k for k in _cache if k.startswith(prefix)]
        for k in stale:
            _cache.pop(k, None)
    return len(stale)


def cached_keys() -> list[str]:
    with _lock:
        return list(_cache)


async def load(db: AsyncSession, tenant_id: uuid.UUID | None) -> Graph:
    version = await graph_version(db)
    key = cache_key(tenant_id, version)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    graph = build(version, await _edges(db))
    with _lock:
        _cache[key] = graph
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return graph


# ================================================================ cycle check
_CYCLE_SQL = text(
    f"""
WITH RECURSIVE e(src, dst) AS (
    SELECT 'global:' || from_skill_id::text, 'global:' || to_skill_id::text
      FROM skill_edges WHERE kind = 'parent_of'
    UNION ALL
    SELECT coalesce('global:' || from_skill_id::text, 'tenant:' || from_tenant_skill_id::text),
           coalesce('global:' || to_skill_id::text, 'tenant:' || to_tenant_skill_id::text)
      FROM tenant_skill_edges WHERE kind = 'parent_of'
    UNION ALL
    SELECT 'global:' || parent_skill_id::text, 'tenant:' || id::text
      FROM tenant_skills WHERE parent_skill_id IS NOT NULL
), walk(node, path, depth) AS (
    SELECT CAST(:start AS text), ARRAY[CAST(:start AS text)], 0
    UNION ALL
    SELECT e.dst, walk.path || e.dst, walk.depth + 1
      FROM walk JOIN e ON e.src = walk.node
     WHERE walk.depth < {CYCLE_DEPTH} AND NOT e.dst = ANY(walk.path)
)
SELECT path FROM walk WHERE node = CAST(:target AS text) ORDER BY depth LIMIT 1
"""
)


async def assert_no_parent_cycle(db: AsyncSession, from_key: str, to_key: str) -> None:
    """Adding ``from parent_of to`` closes a cycle when ``from`` is already reachable from ``to``."""
    path = (await db.execute(_CYCLE_SQL, {"start": to_key, "target": from_key})).scalar_one_or_none()
    if path:
        names = await labels(db, set(path) | {from_key, to_key})
        full = [from_key, *path]
        raise ConflictError(
            "This relationship would create a cycle of parent links",
            code="skill_graph_cycle",
            details={"path": full, "path_names": [names[k].name for k in full]},
        )


# ================================================================ tenant edge CRUD
def _set_end(row: TenantSkillEdge, end: str, key: SkillKey) -> None:
    setattr(row, f"{end}_skill_id", key.id if key.kind == "global" else None)
    setattr(row, f"{end}_tenant_skill_id", key.id if key.kind == "tenant" else None)


async def _approved(db: AsyncSession, raw: str, field: str) -> tuple[SkillKey, SkillLabel]:
    key = parse_key(raw, field)
    label = await _label_of(db, key)
    if label is None:
        raise ValidationFailed(
            "Unknown skill",
            code="unknown_skill",
            details={"fields": [{"field": field, "message": "Unknown skill"}]},
        )
    if label.status != "approved":
        raise ValidationFailed(
            "Only approved skills can have relationships",
            code="skill_not_approved",
            details={"fields": [{"field": field, "message": f"Skill is {label.status}"}]},
        )
    return key, label


async def _edge_out(
    db: AsyncSession, edges: list[Edge], rows: dict[uuid.UUID, Any] | None = None
) -> list[SkillEdgeOut]:
    names = await labels(db, {e.src for e in edges} | {e.dst for e in edges})
    out: list[SkillEdgeOut] = []
    for e in edges:
        row = (rows or {}).get(e.edge_id) if e.edge_id else None
        out.append(
            SkillEdgeOut(
                id=e.edge_id,
                origin=e.origin,  # type: ignore[arg-type]
                from_skill=names[e.src],
                to_skill=names[e.dst],
                kind=e.kind,  # type: ignore[arg-type]
                weight=e.weight,
                note=getattr(row, "note", None),
                source=getattr(row, "source", None) if e.origin == "platform" else e.origin,
                updated_at=getattr(row, "updated_at", None),
            )
        )
    return out


def _tenant_edge(row: TenantSkillEdge) -> Edge:
    return Edge(
        ref_key(row.from_skill_id, row.from_tenant_skill_id),
        ref_key(row.to_skill_id, row.to_tenant_skill_id),
        row.kind,
        Decimal(row.weight),
        "tenant",
        row.id,
    )


def _emit_changed(db: AsyncSession, tenant_id: uuid.UUID, keys: set[str]) -> None:
    emit(db, SkillGraphChanged(tenant_id=tenant_id, scope="tenant", skill_keys=tuple(sorted(keys))))


async def create_edge(ctx: RequestContext, db: AsyncSession, data: SkillEdgeIn) -> SkillEdgeOut:
    assert ctx.tenant_id is not None
    weight = graph_settings.parse_weight(data.weight)
    src, _ = await _approved(db, data.from_key, "from_key")
    dst, _ = await _approved(db, data.to_key, "to_key")
    if src == dst:
        raise ValidationFailed(
            "A skill cannot relate to itself",
            code="skill_edge_self",
            details={"fields": [{"field": "to_key", "message": "Must differ from from_key"}]},
        )
    await graph_settings.locked_row(db, ctx.tenant_id)  # serialise concurrent tenant graph writes
    if data.kind == "parent_of":
        await assert_no_parent_cycle(db, src.as_str(), dst.as_str())
    if await _duplicate(db, src, dst, data.kind):
        raise ConflictError("This relationship already exists", code="skill_edge_exists")
    principal = getattr(ctx.principal, "id", None)
    row = TenantSkillEdge(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        kind=data.kind,
        weight=weight,
        note=data.note,
        created_by=principal,
    )
    _set_end(row, "from", src)
    _set_end(row, "to", dst)
    db.add(row)
    await db.flush()
    audit.record(
        "skill_graph.edge_created",
        "tenant_skill_edge",
        row.id,
        session=db,
        from_key=src.as_str(),
        to_key=dst.as_str(),
        kind=data.kind,
        weight=str(weight),
    )
    _emit_changed(db, ctx.tenant_id, {src.as_str(), dst.as_str()})
    return (await _edge_out(db, [_tenant_edge(row)], {row.id: row}))[0]


async def _duplicate(db: AsyncSession, src: SkillKey, dst: SkillKey, kind: str) -> bool:
    if src.kind == "global" and dst.kind == "global":
        hit = (
            await db.execute(
                select(SkillEdge.id).where(
                    SkillEdge.from_skill_id == src.id, SkillEdge.to_skill_id == dst.id, SkillEdge.kind == kind
                )
            )
        ).first()
        if hit is not None:
            return True
    from_col = TenantSkillEdge.from_skill_id if src.kind == "global" else TenantSkillEdge.from_tenant_skill_id
    to_col = TenantSkillEdge.to_skill_id if dst.kind == "global" else TenantSkillEdge.to_tenant_skill_id
    hit = (
        await db.execute(
            select(TenantSkillEdge.id).where(
                from_col == src.id, to_col == dst.id, TenantSkillEdge.kind == kind
            )
        )
    ).first()
    return hit is not None


async def _tenant_row(db: AsyncSession, edge_id: uuid.UUID) -> TenantSkillEdge:
    row = await db.get(TenantSkillEdge, edge_id)
    if row is None:
        raise NotFound("Relationship not found")
    return row


async def update_edge(
    ctx: RequestContext, db: AsyncSession, edge_id: uuid.UUID, data: SkillEdgeUpdate
) -> SkillEdgeOut:
    assert ctx.tenant_id is not None
    row = await _tenant_row(db, edge_id)
    changes: dict[str, Any] = {}
    if "weight" in data.model_fields_set:
        weight = graph_settings.parse_weight(data.weight)
        if weight != row.weight:
            changes["weight"] = [str(row.weight), str(weight)]
            row.weight = weight
    if "note" in data.model_fields_set and data.note != row.note:
        changes["note"] = True
        row.note = data.note
    if changes:
        await db.flush()
        await db.refresh(row)
        edge = _tenant_edge(row)
        audit.record("skill_graph.edge_updated", "tenant_skill_edge", row.id, session=db, changes=changes)
        _emit_changed(db, ctx.tenant_id, {edge.src, edge.dst})
    return (await _edge_out(db, [_tenant_edge(row)], {row.id: row}))[0]


async def delete_edge(ctx: RequestContext, db: AsyncSession, edge_id: uuid.UUID) -> None:
    assert ctx.tenant_id is not None
    row = await _tenant_row(db, edge_id)
    edge = _tenant_edge(row)
    await db.delete(row)
    await db.flush()
    audit.record(
        "skill_graph.edge_deleted",
        "tenant_skill_edge",
        edge_id,
        session=db,
        from_key=edge.src,
        to_key=edge.dst,
    )
    _emit_changed(db, ctx.tenant_id, {edge.src, edge.dst})


async def list_tenant_edges(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    *,
    key: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[SkillEdgeOut]:
    stmt = (
        select(TenantSkillEdge).order_by(TenantSkillEdge.created_at.desc(), TenantSkillEdge.id).limit(limit)
    )
    if kind is not None:
        stmt = stmt.where(TenantSkillEdge.kind == kind)
    if key is not None:
        k = parse_key(key)
        if k.kind == "global":
            stmt = stmt.where(or_(TenantSkillEdge.from_skill_id == k.id, TenantSkillEdge.to_skill_id == k.id))
        else:
            stmt = stmt.where(
                or_(TenantSkillEdge.from_tenant_skill_id == k.id, TenantSkillEdge.to_tenant_skill_id == k.id)
            )
    rows = list((await db.execute(stmt)).scalars())
    return await _edge_out(db, [_tenant_edge(r) for r in rows], {r.id: r for r in rows})


async def node(ctx: RequestContext, db: AsyncSession, raw_key: str) -> SkillNodeOut:
    key = parse_key(raw_key)
    label = await _label_of(db, key)
    if label is None:
        raise NotFound("Skill not found")
    k = key.as_str()
    graph = await load(db, ctx.tenant_id)
    touching = [e for e in graph.edges if k in (e.src, e.dst)]
    platform_ids = [e.edge_id for e in touching if e.origin == "platform" and e.edge_id]
    tenant_ids = [e.edge_id for e in touching if e.origin == "tenant" and e.edge_id]
    rows: dict[uuid.UUID, Any] = {}
    if platform_ids:
        rows |= {
            r.id: r
            for r in (await db.execute(select(SkillEdge).where(SkillEdge.id.in_(platform_ids)))).scalars()
        }
    if tenant_ids:
        rows |= {
            r.id: r
            for r in (
                await db.execute(select(TenantSkillEdge).where(TenantSkillEdge.id.in_(tenant_ids)))
            ).scalars()
        }
    order = {"tenant": 0, "legacy": 1, "platform": 2}
    touching.sort(key=lambda e: (order[e.origin], e.kind, -e.weight, e.src, e.dst))
    outs = await _edge_out(db, touching, rows)
    if key.kind == "global":
        aliases = list(
            (await db.execute(select(SkillAlias.alias_norm).where(SkillAlias.skill_id == key.id))).scalars()
        )
        aliases += list(
            (
                await db.execute(
                    select(TenantSkillAlias.alias_norm).where(TenantSkillAlias.skill_id == key.id)
                )
            ).scalars()
        )
        parent_id = (await db.execute(select(Skill.parent_id).where(Skill.id == key.id))).scalar_one_or_none()
    else:
        aliases = list(
            (
                await db.execute(
                    select(TenantSkillAlias.alias_norm).where(TenantSkillAlias.tenant_skill_id == key.id)
                )
            ).scalars()
        )
        parent_id = (
            await db.execute(select(TenantSkill.parent_skill_id).where(TenantSkill.id == key.id))
        ).scalar_one_or_none()
    parent = None
    if parent_id is not None:
        parent = (await labels(db, {gkey(parent_id)}))[gkey(parent_id)]
    return SkillNodeOut(
        skill=label,
        aliases=sorted(set(aliases)),
        parent=parent,
        outgoing=[o for o, e in zip(outs, touching, strict=True) if e.src == k],
        incoming=[o for o, e in zip(outs, touching, strict=True) if e.dst == k],
    )
