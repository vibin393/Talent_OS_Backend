# ruff: noqa: E501, D401
"""Skills graph contract (Epic 37; Epic 36 stub, AD-39). The ``expand`` signature is FINAL
(``tests/contracts/signatures.json``).

Public functions (facade ``app.services.skills_service``): ``expand`` (the only graph traversal, AD-41),
``explain_paths``, ``graph_version``, ``recompute`` (the only writer of ``candidate_skills.proficiency`` /
``verification``), ``add_evidence``, plus the graph admin, evidence, suggestion and insight entry points the router
calls (one service function per route).
"""

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.events import emit
from app.core.pagination import Page, PageParams
from app.core.skill_refs import EdgeKind, ExpandedSkill, SkillKey
from app.events.skills import SkillGraphChanged
from app.schemas.candidates import SkillOut
from app.schemas.skills import (
    EvidenceIn,
    EvidenceOut,
    ExpandedSkillOut,
    ExpandPreviewIn,
    ExpandPreviewOut,
    PathHopOut,
    SkillEdgeIn,
    SkillEdgeOut,
    SkillEdgeUpdate,
    SkillGraphSettingsIn,
    SkillGraphSettingsOut,
    SkillInsightsOut,
    SkillNodeOut,
    SkillProfileRow,
    SkillSuggestionOut,
)
from app.services.skills import evidence as evidence_mod
from app.services.skills import graph as graph_mod
from app.services.skills import inference as inference_mod
from app.services.skills import insights as insights_mod
from app.services.skills import recompute as recompute_mod
from app.services.skills import settings as settings_mod
from app.services.skills.expand import check_depth, q2, to_expanded, traverse

__all__ = [
    "DEFAULT_EDGE_KINDS",
    "accept_suggestion",
    "add_evidence",
    "create_edge",
    "delete_edge",
    "expand",
    "expand_preview",
    "explain_paths",
    "get_settings",
    "graph_version",
    "list_edges",
    "list_evidence",
    "list_suggestions",
    "recompute",
    "reject_suggestion",
    "search_skills",
    "skill_insights",
    "skill_node",
    "skill_profile",
    "supersede_evidence",
    "update_edge",
    "update_settings",
]


class _EdgeKinds(frozenset):  # type: ignore[type-arg]
    """A frozenset with a sorted ``repr`` so the frozen signature snapshot is hash-seed independent."""

    def __repr__(self) -> str:
        return "frozenset({" + ", ".join(repr(k) for k in sorted(self)) + "})"


DEFAULT_EDGE_KINDS: frozenset[EdgeKind] = _EdgeKinds({"parent_of", "implies", "related_to"})


# ================================================================ graph contract
async def expand(
    ctx: RequestContext,
    db: AsyncSession,
    refs: Sequence[SkillKey],
    *,
    depth: int = 1,
    kinds: frozenset[EdgeKind] = DEFAULT_EDGE_KINDS,
    min_weight: Decimal = Decimal("0"),
) -> list[ExpandedSkill]:
    """Every input ref (weight 1.00, depth 0) plus the skills that satisfy it (graph traversal only, AD-41).

    Descendants via ``parent_of``, skills that imply it, ``related_to`` either direction; weight = product of
    the edge weights (2 dp, ROUND_HALF_UP); depth 0..2 (else 422 ``depth_out_of_range``); deterministic order.
    """
    check_depth(depth)
    if not refs:
        return []
    graph = await graph_mod.load(db, ctx.tenant_id)
    rows = traverse(graph, [r.as_str() for r in refs], depth, frozenset(kinds), Decimal(min_weight))
    return to_expanded(rows)


async def explain_paths(
    ctx: RequestContext,
    db: AsyncSession,
    refs: Sequence[SkillKey],
    depth: int = 1,
    *,
    min_weight: Decimal = Decimal("0"),
) -> list[ExpandedSkillOut]:
    """``expand`` with labels and the edge path per result (UI explanations, the expansion preview)."""
    check_depth(depth)
    graph = await graph_mod.load(db, ctx.tenant_id)
    rows = traverse(
        graph, [r.as_str() for r in refs], depth, frozenset(DEFAULT_EDGE_KINDS), Decimal(min_weight)
    )
    keys = (
        {r.key for r in rows}
        | {h.edge.src for r in rows for h in r.path}
        | {h.edge.dst for r in rows for h in r.path}
    )
    names = await graph_mod.labels(db, keys)
    verb = {"parent_of": "is a parent of", "implies": "implies", "related_to": "is related to"}
    return [
        ExpandedSkillOut(
            key=r.key,
            name=names[r.key].name,
            root=r.root,
            weight=q2(r.weight),
            depth=r.depth,
            via=r.via,
            path=[
                PathHopOut(
                    from_key=h.edge.src,
                    to_key=h.edge.dst,
                    kind=h.kind,  # type: ignore[arg-type]
                    weight=h.weight,
                    label=f"{names[h.edge.src].name} {verb[h.kind]} {names[h.edge.dst].name}",
                )
                for h in r.path
            ],
        )
        for r in rows
    ]


async def graph_version(ctx: RequestContext, db: AsyncSession) -> str:  # noqa: ARG001
    """Stable hash of the tenant's effective graph (platform seed + tenant edges + legacy parents + settings)."""
    return await graph_mod.graph_version(db)


def invalidate_cache(tenant_id: uuid.UUID) -> int:
    return graph_mod.invalidate(tenant_id)


async def keys_for_ids(db: AsyncSession, ids: list[uuid.UUID]) -> list[SkillKey]:
    return await graph_mod.keys_for_ids(db, ids)


async def matching_settings(db: AsyncSession) -> settings_mod.GraphSettings:
    """``match_depth`` / ``min_weight`` / ``expansion_default`` that matching and search pass to ``expand``."""
    return await settings_mod.current(db)


# ================================================================ graph admin
async def skill_node(ctx: RequestContext, db: AsyncSession, key: str) -> SkillNodeOut:
    return await graph_mod.node(ctx, db, key)


async def search_skills(
    ctx: RequestContext, db: AsyncSession, q: str | None, params: PageParams
) -> Page[SkillOut]:
    from app.services.candidates import skills as catalog

    return await catalog.search_skills(ctx, db, q, include_pending=False, params=params)


async def list_edges(
    ctx: RequestContext, db: AsyncSession, *, key: str | None = None, kind: str | None = None
) -> list[SkillEdgeOut]:
    return await graph_mod.list_tenant_edges(ctx, db, key=key, kind=kind)


async def create_edge(ctx: RequestContext, db: AsyncSession, data: SkillEdgeIn) -> SkillEdgeOut:
    return await graph_mod.create_edge(ctx, db, data)


async def update_edge(
    ctx: RequestContext, db: AsyncSession, edge_id: uuid.UUID, data: SkillEdgeUpdate
) -> SkillEdgeOut:
    return await graph_mod.update_edge(ctx, db, edge_id, data)


async def delete_edge(ctx: RequestContext, db: AsyncSession, edge_id: uuid.UUID) -> None:
    await graph_mod.delete_edge(ctx, db, edge_id)


def _settings_out(cfg: settings_mod.GraphSettings, version: str) -> SkillGraphSettingsOut:
    return SkillGraphSettingsOut(
        expansion_default=cfg.expansion_default,
        match_depth=cfg.match_depth,
        min_weight=cfg.min_weight,
        inference_auto_suggest=cfg.inference_auto_suggest,
        graph_version=version,
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> SkillGraphSettingsOut:  # noqa: ARG001
    return _settings_out(await settings_mod.current(db), await graph_mod.graph_version(db))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: SkillGraphSettingsIn
) -> SkillGraphSettingsOut:
    assert ctx.tenant_id is not None
    row = await settings_mod.locked_row(db, ctx.tenant_id)
    changes: dict[str, Any] = {}
    sent = data.model_fields_set
    if "min_weight" in sent:
        value = settings_mod.parse_weight(data.min_weight, "min_weight")
        if value != row.min_weight:
            changes["min_weight"] = [str(row.min_weight), str(value)]
            row.min_weight = value
    for field in ("expansion_default", "match_depth", "inference_auto_suggest"):
        if field in sent and getattr(data, field) is not None and getattr(data, field) != getattr(row, field):
            changes[field] = [getattr(row, field), getattr(data, field)]
            setattr(row, field, getattr(data, field))
    if changes:
        await db.flush()
        audit.record(
            "skill_graph.settings_updated", "skill_graph_settings", ctx.tenant_id, session=db, changes=changes
        )
        if set(changes) & {"expansion_default", "match_depth", "min_weight"}:
            emit(db, SkillGraphChanged(tenant_id=ctx.tenant_id, scope="tenant", skill_keys=()))
    return _settings_out(await settings_mod.current(db), await graph_mod.graph_version(db))


async def expand_preview(ctx: RequestContext, db: AsyncSession, data: ExpandPreviewIn) -> ExpandPreviewOut:
    refs = [graph_mod.parse_key(r, "refs") for r in data.refs]
    min_weight = (
        settings_mod.parse_weight(data.min_weight, "min_weight")
        if data.min_weight not in (None, "", 0, "0")
        else Decimal(0)
    )
    items = await explain_paths(ctx, db, refs, data.depth, min_weight=min_weight)
    return ExpandPreviewOut(items=items, graph_version=await graph_mod.graph_version(db))


# ================================================================ proficiency and evidence
async def recompute(ctx: RequestContext, db: AsyncSession, candidate_skill_id: uuid.UUID) -> None:  # noqa: ARG001
    """The only writer of ``candidate_skills.proficiency`` / ``verification`` (AD-41)."""
    await recompute_mod.recompute(db, candidate_skill_id)


async def add_evidence(
    ctx: RequestContext, db: AsyncSession, candidate_skill_id: uuid.UUID, data: EvidenceIn
) -> EvidenceOut:
    """Validates the kind / actor rule, inserts, recomputes."""
    return await evidence_mod.add_evidence(ctx, db, candidate_skill_id, data)


async def create_evidence(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    candidate_skill_id: uuid.UUID,
    data: EvidenceIn,
) -> EvidenceOut:
    return await evidence_mod.add_evidence(ctx, db, candidate_skill_id, data, candidate_id=candidate_id)


async def list_evidence(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, candidate_skill_id: uuid.UUID
) -> list[EvidenceOut]:
    return await evidence_mod.list_evidence(ctx, db, candidate_id, candidate_skill_id)


async def supersede_evidence(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    candidate_skill_id: uuid.UUID,
    evidence_id: uuid.UUID,
) -> EvidenceOut:
    return await evidence_mod.supersede(ctx, db, candidate_id, candidate_skill_id, evidence_id)


async def skill_profile(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID
) -> list[SkillProfileRow]:
    return await evidence_mod.profile(ctx, db, candidate_id)


# ================================================================ suggestions
async def list_suggestions(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, status: str | None = "pending"
) -> list[SkillSuggestionOut]:
    return await inference_mod.list_suggestions(ctx, db, candidate_id, status)


async def accept_suggestion(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    proficiency: int | None,
) -> SkillSuggestionOut:
    return await inference_mod.accept(ctx, db, candidate_id, suggestion_id, proficiency)


async def reject_suggestion(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    reason: str | None,
) -> SkillSuggestionOut:
    return await inference_mod.reject(ctx, db, candidate_id, suggestion_id, reason)


async def infer(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, *, graph: bool = True, ai: bool = True
) -> dict[str, Any]:
    """Graph and / or AI skill suggestions for one Candidate (tasks ``skills.infer`` / ``skills.infer_ai``)."""
    out = await inference_mod.infer(ctx, db, candidate_id, graph=graph, ai=ai)
    if graph and not ai:
        out["ai_wanted"] = await inference_mod.ai_wanted(db)
    return out


# ================================================================ insights
async def skill_insights(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    category: str | None = None,
    business_unit_id: uuid.UUID | None = None,
    skill_refs: list[str] | None = None,
    limit: int = 50,
) -> SkillInsightsOut:
    return await insights_mod.skill_insights(
        ctx, db, category=category, business_unit_id=business_unit_id, skill_refs=skill_refs, limit=limit
    )


# ================================================================ event handlers (system context, tasks)
async def on_candidate_skills_changed(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> int:
    return await evidence_mod.ensure_baseline(ctx, db, candidate_id)


async def on_assessment_scored(ctx: RequestContext, db: AsyncSession, event: Any) -> dict[str, int]:
    return await evidence_mod.on_assessment_scored(ctx, db, event)


async def on_project_assignment_ended(ctx: RequestContext, db: AsyncSession, event: Any) -> dict[str, int]:
    return await evidence_mod.on_project_assignment_ended(ctx, db, event)


async def demands_referencing(db: AsyncSession, skill_keys: Sequence[str]) -> list[uuid.UUID]:
    """Open Demands whose skills reference any of ``skill_keys`` (all open Demands when empty)."""
    from app.models.demands import Demand, DemandSkill

    stmt = select(Demand.id).where(Demand.status == "open", Demand.archived_at.is_(None))
    ids = []
    for raw in skill_keys:
        try:
            ids.append(SkillKey.parse(raw).id)
        except ValueError:
            continue
    if skill_keys:
        if not ids:
            return []
        stmt = stmt.where(Demand.id.in_(select(DemandSkill.demand_id).where(DemandSkill.skill_id.in_(ids))))
    return list((await db.execute(stmt.order_by(Demand.id))).scalars())
