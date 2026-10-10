"""Scoring runs (Epic 5, stories 5.4/5.5): demand runs, per-candidate refresh, stale marking, deletion.

``execute_run(ctx, db, run_id)``:
1. pool = candidates in the search projection with ``NOT is_archived AND ai_consent``, minus candidates
   already in the demand's pipeline (``PipelineGateway.candidate_ids_for_demand``);
2. pre-filter = candidates whose skills overlap the expanded required skills (synonyms resolve to the same id;
   children and parents included) UNION ``VectorIndex.search(demand_vector, pool, k=1000)``, capped at 3,000;
3. score (``scoring.score``, pure) in batches of 500; upsert the top 500 plus existing rows (Core upserts:
   derived data, not audited); rows whose ``input_hash`` is unchanged are not rewritten; other non-dismissed
   rows are deleted (dismissed rows and rows already added to the pipeline are kept);
4. funnel ``{total, relevant, strong, available}`` on the run; ``MatchRunCompleted`` (+
   ``RediscoveryCompleted`` on the first run after the demand opened).
"""

import dataclasses
import hashlib
import uuid
from datetime import UTC, date, datetime
from typing import Any

from loguru import logger
from sqlalchemy import Uuid as SAUuid
from sqlalchemy import bindparam, case, delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.core.skill_refs import SkillKey
from app.events.matching import MatchRunCompleted, MatchRunRequested, RediscoveryCompleted
from app.models.matching import Match, MatchRun
from app.models.search import CandidateSearchDoc
from app.schemas.matching_profiles import (
    CandidateMatchProfile,
    DemandMatchProfile,
    SkillExpansion,
    SkillGraph,
    null_proxies,
)
from app.services import embeddings_service
from app.services.ai import embedding_store
from app.services.ai.vector_index import cosine
from app.services.candidates import search_port as candidates
from app.services.demands import matching_port as demands
from app.services.matching import scoring
from app.services.matching.ports import pipeline_gateway
from app.services.matching.settings import EffectiveSettings, effective

KEEP_TOP = 500
VECTOR_K = 1000
PREFILTER_CAP = 3000
BATCH = 500
AVAILABLE_MIN = 80
_UUIDS = ARRAY(SAUuid(as_uuid=True))
_MATCHES = Match.__table__
_DOCS = CandidateSearchDoc.__table__


def _now() -> datetime:
    return datetime.now(UTC)


def _today() -> date:
    return _now().date()


@dataclasses.dataclass
class Scored:
    candidate_id: uuid.UUID
    result: scoring.ScoreResult
    input_hash: str
    similarity: float | None


# ================================================================ run lifecycle
async def request_run(db: AsyncSession, tenant_id: uuid.UUID, demand_id: uuid.UUID, trigger: str) -> MatchRun:
    """Create a ``running`` run row; after commit the handler enqueues ``matching.tasks.run_for_demand``."""
    run = MatchRun(
        id=uuid.uuid4(), tenant_id=tenant_id, demand_id=demand_id, trigger=trigger, status="running"
    )
    db.add(run)
    await db.flush()
    emit(db, MatchRunRequested(tenant_id=tenant_id, demand_id=demand_id, run_id=run.id, trigger=trigger))
    return run


async def fail_run(db: AsyncSession, run_id: uuid.UUID, error: str) -> None:
    await db.execute(
        update(MatchRun)
        .where(MatchRun.id == run_id, MatchRun.status == "running")
        .values(status="failed", finished_at=_now(), error=error[:200])
    )


def _expanded_skill_ids(demand: DemandMatchProfile, graph: SkillGraph) -> set[uuid.UUID]:
    out: set[uuid.UUID] = set()
    for req in demand.skills:
        if req.skill_id is None:
            continue
        out.add(req.skill_id)
        if not req.expand:
            continue
        out |= graph.descendants_of(req.skill_id)
        parent = graph.parent_of(req.skill_id)
        if parent is not None:
            out.add(parent)
    for exp in demand.expansions:  # Epic 37: the same expansion scoring uses
        out |= {sid for sid, _, _ in exp.neighbours}
    return out


_VERB = {"implies": "implies", "parent_of": "child of", "related_to": "related to"}


async def with_expansions(
    ctx: RequestContext, db: AsyncSession, demand: DemandMatchProfile
) -> tuple[DemandMatchProfile, str | None]:
    """Epic 37 (FR-190, AD-8): expand the Demand's ``expand=True`` skills through ``skills_service`` (the only
    graph traversal, reached through ``core.skill_graph_port``) BEFORE scoring and attach them as data. Returns
    (profile, graph version for the hash); without a provider the profile is unchanged (P1 behaviour)."""
    from app.core.skill_graph_port import skill_graph

    skills_service = skill_graph()
    if skills_service is None:
        return demand, None
    version = await skills_service.graph_version(ctx, db)
    refs = [
        SkillKey(kind="tenant" if r.skill_ref_kind == "tenant" else "global", id=r.skill_id)
        for r in demand.skills
        if r.skill_id is not None and r.expand
    ]
    if not refs:
        return demand, version
    cfg = await skills_service.matching_settings(db)
    rows = await skills_service.explain_paths(ctx, db, refs, cfg.match_depth, min_weight=cfg.min_weight)
    roots = {r.key: r.name for r in rows if r.depth == 0}
    names = {r.key: r.name for r in rows}
    grouped: dict[uuid.UUID, list[tuple[uuid.UUID, Any, str]]] = {}
    for r in rows:
        if r.depth == 0:
            continue
        root_id = SkillKey.parse(r.root).id
        if r.depth == 1:
            label = f"{_VERB.get(r.path[0].kind, 'related to')} {roots.get(r.root, '')}".strip()
        else:
            mid = r.path[0].from_key if r.path[0].to_key == r.root else r.path[0].to_key
            label = f"via {names.get(mid) or roots.get(r.root, '')}"
        grouped.setdefault(root_id, []).append((SkillKey.parse(r.key).id, r.weight, label))
    expansions = tuple(
        SkillExpansion(required_skill_id=rid, neighbours=tuple(sorted(n, key=lambda x: (-x[1], str(x[0])))))
        for rid, n in sorted(grouped.items(), key=lambda kv: str(kv[0]))
    )
    return dataclasses.replace(demand, expansions=expansions), version


async def _pool(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await db.execute(
        select(_DOCS.c.candidate_id).where(_DOCS.c.is_archived.is_(False), _DOCS.c.ai_consent.is_(True))
    )
    in_pipeline = await pipeline_gateway().candidate_ids_for_demand(ctx, db, demand_id)
    return [r[0] for r in rows if r[0] not in in_pipeline]


async def _prefilter(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    pool: list[uuid.UUID],
    skill_ids: set[uuid.UUID],
    demand_vec: list[float] | None,
) -> list[uuid.UUID]:
    if not pool:
        return []
    pool_set = set(pool)
    by_skill: list[uuid.UUID] = []
    if skill_ids:
        rows = await db.execute(
            select(_DOCS.c.candidate_id)
            .where(
                _DOCS.c.is_archived.is_(False),
                _DOCS.c.ai_consent.is_(True),
                _DOCS.c.skill_ids.op("&&")(bindparam(None, sorted(skill_ids, key=str), type_=_UUIDS)),
            )
            .order_by(_DOCS.c.candidate_id)
        )
        by_skill = [r[0] for r in rows if r[0] in pool_set]
    by_vector: list[uuid.UUID] = []
    if demand_vec is not None:
        by_vector = [
            cid for cid, _ in await embedding_store.search(db, tenant_id, demand_vec, pool, VECTOR_K)
        ]
    merged = list(dict.fromkeys([*by_skill, *by_vector]))
    return merged[:PREFILTER_CAP]


async def _similarities(
    db: AsyncSession, demand_vec: list[float] | None, ids: list[uuid.UUID]
) -> dict[uuid.UUID, float]:
    if demand_vec is None or not ids:
        return {}
    vectors = await embedding_store.candidate_vectors(db, ids)
    return {cid: cosine(demand_vec, vec) for cid, vec in vectors.items()}


def score_profiles(
    demand: DemandMatchProfile,
    profiles: list[CandidateMatchProfile],
    sims: dict[uuid.UUID, float],
    settings: EffectiveSettings,
    graph: SkillGraph,
    today: date,
    graph_version: str | None = None,
) -> list[Scored]:
    out: list[Scored] = []
    for raw in profiles:
        profile = null_proxies(raw, settings.proxy_fields)
        sim = sims.get(profile.candidate_id)
        result = scoring.score(
            demand,
            profile,
            weights=settings.weights,
            graph=graph,
            similarity=sim,
            fx=settings.fx,
            today=today,
        )
        digest = scoring.input_hash(
            demand,
            profile,
            weights=settings.weights,
            similarity=sim,
            fx=settings.fx,
            today=today,
            graph_version=graph_version,
        )
        out.append(Scored(profile.candidate_id, result, digest, sim))
    return out


async def _existing(db: AsyncSession, demand_id: uuid.UUID) -> dict[uuid.UUID, Any]:
    rows = await db.execute(
        select(
            _MATCHES.c.candidate_id,
            _MATCHES.c.id,
            _MATCHES.c.input_hash,
            _MATCHES.c.dismissed_at,
            _MATCHES.c.is_stale,
            _MATCHES.c.added_to_pipeline_at,
        ).where(_MATCHES.c.demand_id == demand_id)
    )
    return {r.candidate_id: r for r in rows}


def _row_values(tenant_id: uuid.UUID, demand_id: uuid.UUID, s: Scored, now: datetime) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "demand_id": demand_id,
        "candidate_id": s.candidate_id,
        "overall": s.result.overall,
        "sub_scores": dict(s.result.sub_scores),
        "evidence": [e.as_dict() for e in s.result.evidence],
        "missing_mandatory": list(s.result.missing_mandatory),
        "scoring_version": scoring.SCORING_VERSION,
        "input_hash": s.input_hash,
        "computed_at": now,
        "explanation_status": "pending",
        "is_stale": False,
        "created_at": now,
        "updated_at": now,
    }


async def upsert_scores(
    db: AsyncSession, tenant_id: uuid.UUID, demand_id: uuid.UUID, rows: list[Scored]
) -> int:
    if not rows:
        return 0
    now = _now()
    written = 0
    for start in range(0, len(rows), 200):
        chunk = rows[start : start + 200]
        stmt = insert(_MATCHES).values([_row_values(tenant_id, demand_id, s, now) for s in chunk])
        ex = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["demand_id", "candidate_id"],
            set_={
                "overall": ex.overall,
                "sub_scores": ex.sub_scores,
                "evidence": ex.evidence,
                "missing_mandatory": ex.missing_mandatory,
                "scoring_version": ex.scoring_version,
                "input_hash": ex.input_hash,
                "computed_at": ex.computed_at,
                "is_stale": literal(False),
                "updated_at": ex.updated_at,
                "explanation_status": case(
                    (_MATCHES.c.explanation_input_hash == ex.input_hash, _MATCHES.c.explanation_status),
                    else_=literal("pending"),
                ),
            },
        )
        await db.execute(stmt)
        written += len(chunk)
    return written


def _run_hash(scored: list[Scored], settings: EffectiveSettings) -> str:
    digest = hashlib.sha256(scoring.SCORING_VERSION.encode())
    digest.update(repr(sorted(settings.weights.items())).encode())
    for s in sorted(scored, key=lambda x: str(x.candidate_id)):
        digest.update(f"{s.candidate_id}:{s.input_hash};".encode())
    return digest.hexdigest()


async def execute_run(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> MatchRun | None:
    run = await db.get(MatchRun, run_id)
    if run is None or run.status != "running":
        return run
    demand_id, tenant_id = run.demand_id, run.tenant_id
    status = await demands.demand_status(db, demand_id)
    if status != "open":
        run.status, run.finished_at = "skipped", _now()
        await db.flush()
        return run
    settings = await effective(db)
    graph = await candidates.skill_graph(db)
    demand = await demands.get_matching_profile(db, demand_id)
    if demand is None:  # pragma: no cover - deleted meanwhile
        run.status, run.finished_at = "skipped", _now()
        await db.flush()
        return run
    demand, graph_version = await with_expansions(ctx, db, demand)
    demand_vec = await embeddings_service.sync_demand(db, tenant_id, demand_id)
    pool = await _pool(ctx, db, demand_id)
    prefiltered = await _prefilter(db, tenant_id, pool, _expanded_skill_ids(demand, graph), demand_vec)
    today = _today()
    scored: list[Scored] = []
    for start in range(0, len(prefiltered), BATCH):
        chunk = prefiltered[start : start + BATCH]
        profiles = await candidates.get_matching_profiles(db, chunk)
        sims = await _similarities(db, demand_vec, [p.candidate_id for p in profiles])
        scored += score_profiles(demand, profiles, sims, settings, graph, today, graph_version)

    existing = await _existing(db, demand_id)
    ranked = sorted(scored, key=lambda s: (-s.result.overall, str(s.candidate_id)))
    keep = {s.candidate_id for s in ranked[:KEEP_TOP]} | {
        s.candidate_id for s in scored if s.candidate_id in existing
    }
    to_write = [
        s
        for s in scored
        if s.candidate_id in keep
        and (
            s.candidate_id not in existing
            or existing[s.candidate_id].input_hash != s.input_hash
            or existing[s.candidate_id].is_stale
        )
    ]
    await upsert_scores(db, tenant_id, demand_id, to_write)
    stale_ids = [
        cid
        for cid, row in existing.items()
        if cid not in keep and row.dismissed_at is None and row.added_to_pipeline_at is None
    ]
    if stale_ids:
        await db.execute(delete(Match).where(Match.demand_id == demand_id, Match.candidate_id.in_(stale_ids)))
    await db.execute(
        update(Match).where(Match.demand_id == demand_id, Match.is_stale.is_(True)).values(is_stale=False)
    )

    strong = [s for s in scored if s.result.overall >= settings.strong_threshold]
    funnel = {
        "total": len(pool),
        "relevant": sum(
            1
            for s in scored
            if s.result.credited_skills >= 1
            or (s.similarity is not None and 100 * s.similarity >= settings.relevant_threshold)
        ),
        "strong": len(strong),
        "available": sum(
            1 for s in strong if (s.result.sub_scores.get("availability") or 0) >= AVAILABLE_MIN
        ),
        "strong_threshold": settings.strong_threshold,
    }
    first_rediscovery = (
        run.trigger == "opened"
        and (
            await db.execute(
                select(func.count())
                .select_from(MatchRun)
                .where(
                    MatchRun.demand_id == demand_id,
                    MatchRun.status == "completed",
                    MatchRun.trigger == "opened",
                    MatchRun.id != run.id,
                )
            )
        ).scalar_one()
        == 0
    )
    run.status = "completed"
    run.finished_at = _now()
    run.considered = len(scored)
    run.funnel = funnel
    run.input_hash = _run_hash(scored, settings)
    await db.flush()
    emit(db, MatchRunCompleted(tenant_id=tenant_id, demand_id=demand_id, run_id=run.id, status="completed"))
    if first_rediscovery:
        assignees = await demands.get_assignees(db, demand_id)
        emit(
            db,
            RediscoveryCompleted(
                tenant_id=tenant_id,
                demand_id=demand_id,
                run_id=run.id,
                funnel=funnel,
                assignee_ids=tuple(assignees),
            ),
        )
    logger.info(
        "Match run {} demand={} pool={} prefiltered={} strong={}",
        run.id,
        demand_id,
        len(pool),
        len(scored),
        len(strong),
    )
    return run


# ================================================================ candidate refresh
async def delete_candidate_matches(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> int:
    if not candidate_ids:
        return 0
    result = await db.execute(delete(Match).where(Match.candidate_id.in_(candidate_ids)))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _threshold(db: AsyncSession, demand_id: uuid.UUID) -> float | None:
    """Return the demand's 500th best (non-dismissed) score, or None when it has fewer rows."""
    value = (
        await db.execute(
            select(_MATCHES.c.overall)
            .where(_MATCHES.c.demand_id == demand_id, _MATCHES.c.dismissed_at.is_(None))
            .order_by(_MATCHES.c.overall.desc())
            .offset(KEEP_TOP - 1)
            .limit(1)
        )
    ).scalar_one_or_none()
    return float(value) if value is not None else None


async def score_candidate_against_open_demands(
    ctx: RequestContext, db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> int:
    """Refresh one candidate's matches for every Open demand. Returns rows written."""
    open_ids = await demands.list_open_demand_ids(db)
    if not open_ids:
        return 0
    eligible = candidate_id in await candidates.ai_consent_active(db, [candidate_id])
    profiles = await candidates.get_matching_profiles(db, [candidate_id]) if eligible else []
    if not profiles:  # no consent / archived / merged / erased: no matches at all
        await delete_candidate_matches(db, [candidate_id])
        return 0
    settings = await effective(db)
    graph = await candidates.skill_graph(db)
    today = _today()
    cand_vec = (await embedding_store.candidate_vectors(db, [candidate_id])).get(candidate_id)
    written = 0
    for demand_id in open_ids:
        if candidate_id in await pipeline_gateway().candidate_ids_for_demand(ctx, db, demand_id):
            continue
        demand = await demands.get_matching_profile(db, demand_id)
        if demand is None:
            continue
        demand, graph_version = await with_expansions(ctx, db, demand)
        demand_vec = await embedding_store.demand_vector(db, demand_id)
        sims = (
            {candidate_id: cosine(demand_vec, cand_vec)}
            if demand_vec is not None and cand_vec is not None
            else {}
        )
        (s,) = score_profiles(demand, profiles, sims, settings, graph, today, graph_version)
        row = await _existing_one(db, demand_id, candidate_id)
        if row is not None:
            if row.input_hash != s.input_hash:
                written += await upsert_scores(db, tenant_id, demand_id, [s])
            continue
        threshold = await _threshold(db, demand_id)
        if threshold is None or float(s.result.overall) >= threshold:
            written += await upsert_scores(db, tenant_id, demand_id, [s])
    return written


async def _existing_one(db: AsyncSession, demand_id: uuid.UUID, candidate_id: uuid.UUID) -> Any:
    return (
        await db.execute(
            select(_MATCHES.c.id, _MATCHES.c.input_hash).where(
                _MATCHES.c.demand_id == demand_id, _MATCHES.c.candidate_id == candidate_id
            )
        )
    ).first()


async def mark_stale(db: AsyncSession, demand_id: uuid.UUID) -> int:
    result = await db.execute(
        update(Match).where(Match.demand_id == demand_id, Match.is_stale.is_(False)).values(is_stale=True)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
