"""Candidate search (Epic 5, stories 5.1 / 5.3): Boolean + faceted + natural-language search over
``candidate_search_docs``.

* ``boolean`` -- ``query_parser`` AST -> one tsquery built from **bound parameters only**: each leaf is
  ``to_tsquery('talent_en', :p)`` of sanitised lexemes (``:*`` for prefix terms) or ``phraseto_tsquery`` for
  phrases, combined with ``tsquery_and / tsquery_or / tsquery_not`` (= ``&& || !!``). A single-atom query
  also matches names by trigram similarity (>= 0.35). Rank = ``ts_rank_cd(doc, q, 32) + 0.5 * similarity``.
* ``structured`` -- same, plus optional ``semantic_text`` ranking (no AI call).
* ``nl`` -- the AI gateway turns the query into filters (``nl_search_filters.v1``); filters + keyword rank +
  semantic rank (``0.6 * cosine_norm + 0.4 * fts_norm``). AI unavailable -> ``websearch_to_tsquery`` keyword
  search with ``notice: ai_unavailable`` (never a 5xx).

Facets: each multi-select facet counts with every other filter applied but not its own (one UNION ALL
statement over a flagged CTE). Over 800 ms the facets are recomputed on ``TABLESAMPLE SYSTEM(10)`` and
flagged ``approximate``. Compensation filters need ``compensation:view`` (else 403 ``forbidden_filter``).
Snippets are ``ts_headline`` segments ``[{text, hl}]`` -- the API never returns HTML.
"""

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import (
    ColumnElement,
    Table,
    and_,
    bindparam,
    case,
    cast,
    func,
    literal,
    literal_column,
    not_,
    or_,
    select,
    tablesample,
    text,
    true,
    union_all,
)
from sqlalchemy import Text as SAText
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import Forbidden, ValidationFailed
from app.core.permissions import Principal, can_view_compensation, scope_condition
from app.models.search import CandidateSearchDoc
from app.schemas.matching_profiles import annualise
from app.schemas.search import (
    CandidateHit,
    CandidateSearchFilters,
    CandidateSearchIn,
    CandidateSearchOut,
    Facet,
    FacetBucket,
    InterpretationChip,
    NLInterpretation,
    SearchSkillOut,
    SnippetSegment,
)
from app.services.candidates import search_port as candidates
from app.services.matching import settings as matching_settings
from app.services.matching.scoring import convert
from app.services.search import query_parser as qp

READ = "candidate:read"
CFG = literal_column("'talent_en'::regconfig")
NAME_SIMILARITY = 0.35
FACET_TIMEOUT_MS = 800
FACET_EXACT_MAX = 50_000  # above this many hits facets are computed on a 10 % sample
FACET_TOP = 25
SAMPLE_PERCENT = 10
SEMANTIC_WINDOW = 1000
HEADLINE_OPTS = 'StartSel="\x01", StopSel="\x02", MaxWords=30, MinWords=12, MaxFragments=2'
MULTI_FACETS = ("skills", "country", "city", "certifications", "work_auth", "source", "owner", "tags")
BUCKET_FACETS = ("experience", "notice")
EXPERIENCE_BUCKETS = (("0-2", 0, 2), ("2-5", 2, 5), ("5-8", 5, 8), ("8-12", 8, 12), ("12+", 12, None))
NOTICE_BUCKETS = (
    ("0", 0, 0),
    ("1-15", 1, 15),
    ("16-30", 16, 30),
    ("31-60", 31, 60),
    ("61-90", 61, 90),
    ("90+", 91, None),
)
_UUIDS = ARRAY(SAUuid(as_uuid=True))
_TEXTS = ARRAY(SAText())
_DOCS: Table = CandidateSearchDoc.__table__  # type: ignore[assignment]


def invalid_query(err: qp.QuerySyntaxError) -> ValidationFailed:
    return ValidationFailed(
        err.message, code="invalid_query", details={"position": err.position, "message": err.message}
    )


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal):  # pragma: no cover - routes authenticate
        raise Forbidden()
    return principal


# ================================================================ tsquery (bound parameters only)
def _leaf_term(term: qp.Term) -> Any:
    lex = qp.lexemes(term.text)
    if not lex:  # pragma: no cover - the tokenizer rejects terms without letters/digits
        return func.to_tsquery(CFG, "")
    if term.prefix:
        lex[-1] = lex[-1] + ":*"
    return func.to_tsquery(CFG, bindparam(None, " & ".join(lex), type_=SAText()))


def tsquery_of(node: qp.Node) -> Any:
    if isinstance(node, qp.Term):
        return _leaf_term(node)
    if isinstance(node, qp.Phrase):
        return func.phraseto_tsquery(CFG, bindparam(None, node.text, type_=SAText()))
    if isinstance(node, qp.Not):
        return func.tsquery_not(tsquery_of(node.child))
    parts = [tsquery_of(c) for c in node.children]
    combine = func.tsquery_and if isinstance(node, qp.And) else func.tsquery_or
    acc = parts[0]
    for part in parts[1:]:
        acc = combine(acc, part)
    return acc


@dataclasses.dataclass
class TextQuery:
    tsquery: Any | None = None
    name_q: str | None = None  # single-atom query text for the trigram name fallback
    terms: tuple[str, ...] = ()


def text_query(q: str | None, *, websearch: bool = False) -> TextQuery:
    raw = (q or "").strip()
    if not raw:
        return TextQuery()
    if websearch:
        return TextQuery(
            tsquery=func.websearch_to_tsquery(CFG, bindparam(None, raw[: qp.MAX_LENGTH], type_=SAText())),
            terms=tuple(qp.lexemes(raw)),
        )
    try:
        node = qp.parse(raw)
    except qp.QuerySyntaxError as err:
        raise invalid_query(err) from err
    name_q = node.text if qp.is_single_atom(node) else None  # type: ignore[union-attr]
    return TextQuery(tsquery=tsquery_of(node), name_q=name_q, terms=tuple(qp.positive_terms(node)))


# ================================================================ filters
def _arr(values: list[Any], type_: Any) -> Any:
    return bindparam(None, values, type_=type_)


@dataclasses.dataclass
class Plan:
    filters: CandidateSearchFilters
    text: TextQuery
    skill_sets: list[set[uuid.UUID]]
    comp_range_inr: tuple[Decimal | None, Decimal | None]

    @property
    def expanded_skills(self) -> set[uuid.UUID]:
        out: set[uuid.UUID] = set()
        for s in self.skill_sets:
            out |= s
        return out


def facet_conditions(t: Any, plan: Plan) -> dict[str, ColumnElement[bool] | None]:
    f = plan.filters
    c = t.c
    conds: dict[str, ColumnElement[bool] | None] = dict.fromkeys((*MULTI_FACETS, *BUCKET_FACETS))
    if plan.skill_sets:
        if f.skills_mode == "all":
            conds["skills"] = and_(
                *[c.skill_ids.op("&&")(_arr(sorted(s, key=str), _UUIDS)) for s in plan.skill_sets]
            )
        else:
            conds["skills"] = c.skill_ids.op("&&")(_arr(sorted(plan.expanded_skills, key=str), _UUIDS))
    if f.countries:
        conds["country"] = func.lower(c.country).in_([x.lower() for x in f.countries])
    if f.cities:
        conds["city"] = func.lower(c.city).in_([x.lower() for x in f.cities])
    if f.certifications:
        conds["certifications"] = c.certifications.op("&&")(_arr(list(f.certifications), _TEXTS))
    if f.work_auth:
        conds["work_auth"] = c.work_auth.op("&&")(_arr(list(f.work_auth), _TEXTS))
    if f.sources:
        conds["source"] = c.source.in_(list(f.sources))
    if f.owner_ids:
        conds["owner"] = c.owner_id.in_(list(f.owner_ids))
    if f.tags:
        conds["tags"] = c.tags.op("&&")(_arr(list(f.tags), _UUIDS))
    exp: list[ColumnElement[bool]] = []
    if f.experience_min is not None:
        exp.append(c.experience_years >= f.experience_min)
    if f.experience_max is not None:
        exp.append(c.experience_years <= f.experience_max)
    if exp:
        conds["experience"] = and_(*exp)
    if f.notice_max_days is not None:
        conds["notice"] = c.notice_days <= f.notice_max_days
    return conds


def other_conditions(t: Any, plan: Plan) -> list[ColumnElement[bool]]:
    f = plan.filters
    c = t.c
    out: list[ColumnElement[bool]] = []
    lo, hi = plan.comp_range_inr
    if lo is not None:
        out.append(c.expected_comp_inr >= lo)
    if hi is not None:
        out.append(c.expected_comp_inr <= hi)
    if f.available_by is not None:
        out.append(
            or_(
                c.available_from <= f.available_by,
                and_(
                    c.available_from.is_(None),
                    c.notice_days.is_not(None),
                    func.current_date() + c.notice_days <= f.available_by,
                ),
            )
        )
    if f.current_employers:
        out.append(func.lower(c.current_employer).in_([x.lower() for x in f.current_employers]))
    if f.updated_after is not None:
        out.append(c.candidate_updated_at >= f.updated_after)
    if f.updated_before is not None:
        out.append(c.candidate_updated_at <= f.updated_before)
    if f.not_submitted_to_client_id is not None:
        out.append(not_(c.submitted_client_ids.op("@>")(_arr([f.not_submitted_to_client_id], _UUIDS))))
    return out


def text_condition(t: Any, tq: TextQuery) -> ColumnElement[bool] | None:
    if tq.tsquery is None:
        return None
    match = t.c.doc.op("@@")(tq.tsquery)
    if tq.name_q:
        qn = func.f_unaccent(func.lower(bindparam(None, tq.name_q, type_=SAText())))
        match = or_(
            match, and_(t.c.name_norm.op("%")(qn), func.similarity(t.c.name_norm, qn) >= NAME_SIMILARITY)
        )
    return match


def rank_expr(t: Any, tq: TextQuery) -> Any:
    if tq.tsquery is None:
        return literal(0.0)
    rank = func.ts_rank_cd(t.c.doc, tq.tsquery, 32)
    if tq.name_q:
        qn = func.f_unaccent(func.lower(bindparam(None, tq.name_q, type_=SAText())))
        rank = rank + 0.5 * func.similarity(t.c.name_norm, qn)
    return rank


def base_conditions(t: Any, principal: Principal, tq: TextQuery) -> list[ColumnElement[bool]]:
    out: list[ColumnElement[bool]] = [t.c.is_archived.is_(False)]
    scope = scope_condition(principal, READ, t.c.owner_id)
    if scope is not None:
        out.append(scope)
    match = text_condition(t, tq)
    if match is not None:
        out.append(match)
    return out


# ================================================================ facets
def _bucket_case(col: Any, buckets: tuple[tuple[str, int, int | None], ...]) -> Any:
    whens = []
    for label, lo, hi in buckets:
        cond = col >= lo if hi is None else and_(col >= lo, col < hi) if hi != lo else col == lo
        whens.append((cond, label))
    return case(*whens, else_=None)


def _notice_case(col: Any) -> Any:
    whens = []
    for label, lo, hi in NOTICE_BUCKETS:
        whens.append((col >= lo if hi is None else and_(col >= lo, col <= hi), label))
    return case(*whens, else_=None)


def facets_statement(t: Any, principal: Principal, plan: Plan, *, scale: int = 1) -> Any:
    conds = facet_conditions(t, plan)
    flags = [(cond if cond is not None else true()).label(f"f_{key}") for key, cond in conds.items()]
    base = (
        select(
            t.c.candidate_id,
            t.c.skill_ids,
            t.c.skill_names,
            t.c.country,
            t.c.city,
            t.c.certifications,
            t.c.work_auth,
            t.c.source,
            t.c.owner_id,
            t.c.tags,
            t.c.experience_years,
            t.c.notice_days,
            *flags,
        )
        .where(*base_conditions(t, principal, plan.text), *other_conditions(t, plan))
        .cte("facet_base")
    )

    def others(key: str) -> ColumnElement[bool]:
        parts = [base.c[f"f_{k}"] for k in conds if k != key]
        return and_(*parts) if parts else true()

    count = (func.count() * scale).label("n")
    selects = []

    def top(key: str, value: Any, label: Any, *, from_: Any = None, limit: int | None = FACET_TOP) -> None:
        stmt = (
            select(
                literal(key).label("facet"),
                cast(value, SAText()).label("value"),
                cast(label, SAText()).label("label"),
                count,
            )
            .select_from(from_ if from_ is not None else base)
            .where(others(key), value.is_not(None))
            .group_by(value, label)
            .order_by(func.count().desc(), cast(value, SAText()))
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        selects.append(select(stmt.subquery()))

    skills_u = (
        func.unnest(base.c.skill_ids, base.c.skill_names).table_valued("sid", "sname").render_derived("su")
    )
    top("skills", skills_u.c.sid, skills_u.c.sname, from_=base.join(skills_u, true()))
    top("country", base.c.country, base.c.country)
    top("city", base.c.city, base.c.city)
    certs_u = func.unnest(base.c.certifications).table_valued("cert").render_derived("cu")
    top("certifications", certs_u.c.cert, certs_u.c.cert, from_=base.join(certs_u, true()))
    auth_u = func.unnest(base.c.work_auth).table_valued("auth").render_derived("wu")
    top("work_auth", auth_u.c.auth, auth_u.c.auth, from_=base.join(auth_u, true()), limit=None)
    top("source", base.c.source, base.c.source, limit=None)
    top("owner", base.c.owner_id, base.c.owner_id, limit=None)
    tags_u = func.unnest(base.c.tags).table_valued("tag").render_derived("tu")
    top("tags", tags_u.c.tag, tags_u.c.tag, from_=base.join(tags_u, true()), limit=None)
    exp = _bucket_case(base.c.experience_years, EXPERIENCE_BUCKETS)
    top("experience", exp, exp, limit=None)
    notice = _notice_case(base.c.notice_days)
    top("notice", notice, notice, limit=None)
    return union_all(*selects)


_BUCKET_ORDER = {"experience": [b[0] for b in EXPERIENCE_BUCKETS], "notice": [b[0] for b in NOTICE_BUCKETS]}


async def _facet_rows(
    db: AsyncSession, principal: Principal, plan: Plan, total: int | None = None
) -> tuple[list[Any], bool]:
    previous = (await db.execute(text("SELECT current_setting('statement_timeout')"))).scalar_one()
    if total is not None and total > FACET_EXACT_MAX:
        # Large result sets go straight to the sample instead of spending the 800 ms budget first.
        sampled = tablesample(_DOCS, func.system(SAMPLE_PERCENT), name="docs_sample")
        return list(
            await db.execute(facets_statement(sampled, principal, plan, scale=100 // SAMPLE_PERCENT))
        ), True
    try:
        async with db.begin_nested():
            await db.execute(
                text("SELECT set_config('statement_timeout', :v, true)"), {"v": str(FACET_TIMEOUT_MS)}
            )
            rows = list(await db.execute(facets_statement(_DOCS, principal, plan)))
            await db.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": previous})
        return rows, False
    except DBAPIError as exc:
        if "statement timeout" not in str(exc).lower() and "canceling statement" not in str(exc).lower():
            raise
        logger.warning(
            "Facet query exceeded {} ms; recomputing on a {}% sample", FACET_TIMEOUT_MS, SAMPLE_PERCENT
        )
    await db.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": previous})
    sampled = tablesample(_DOCS, func.system(SAMPLE_PERCENT), name="docs_sample")
    rows = list(await db.execute(facets_statement(sampled, principal, plan, scale=100 // SAMPLE_PERCENT)))
    return rows, True


async def compute_facets(
    db: AsyncSession, principal: Principal, plan: Plan, total: int | None = None
) -> tuple[list[Facet], bool]:
    rows, approximate = await _facet_rows(db, principal, plan, total)
    grouped: dict[str, list[FacetBucket]] = {k: [] for k in (*MULTI_FACETS, *BUCKET_FACETS)}
    for facet, value, label, n in rows:
        grouped[facet].append(FacetBucket(value=str(value), label=str(label), count=int(n)))
    owners = await candidates.owner_names(db, {uuid.UUID(b.value) for b in grouped["owner"]})
    for b in grouped["owner"]:
        b.label = owners.get(uuid.UUID(b.value), b.label)
    tags = await candidates.tag_names(db, {uuid.UUID(b.value) for b in grouped["tags"]})
    for b in grouped["tags"]:
        b.label = tags.get(uuid.UUID(b.value), b.label)
    for key, order in _BUCKET_ORDER.items():
        grouped[key].sort(key=lambda b, o=order: o.index(b.value) if b.value in o else 99)  # type: ignore[misc]
    return [Facet(key=k, buckets=v, approximate=approximate) for k, v in grouped.items()], approximate


# ================================================================ results
def _order(t: Any, sort: str, tq: TextQuery) -> list[Any]:
    if sort == "updated" or (sort == "relevance" and tq.tsquery is None):
        return [t.c.candidate_updated_at.desc(), t.c.candidate_id]
    if sort == "experience":
        return [t.c.experience_years.desc().nulls_last(), t.c.candidate_updated_at.desc(), t.c.candidate_id]
    return [literal_column("rank").desc(), t.c.candidate_updated_at.desc(), t.c.candidate_id]


def _where(t: Any, principal: Principal, plan: Plan) -> list[ColumnElement[bool]]:
    conds = [c for c in facet_conditions(t, plan).values() if c is not None]
    return [*base_conditions(t, principal, plan.text), *conds, *other_conditions(t, plan)]


def _segments(raw: str | None) -> list[SnippetSegment]:
    if not raw:
        return []
    out: list[SnippetSegment] = []
    hl = False
    buf = ""
    for ch in raw:
        if ch in "\x01\x02":
            if buf:
                out.append(SnippetSegment(text=buf, hl=hl))
            buf = ""
            hl = ch == "\x01"
            continue
        buf += ch
    if buf:
        out.append(SnippetSegment(text=buf, hl=hl))
    return out


async def _snippets(
    db: AsyncSession, ids: list[uuid.UUID], tq: TextQuery
) -> dict[uuid.UUID, list[SnippetSegment]]:
    if not ids or tq.tsquery is None:
        return {}
    t = _DOCS
    source = func.concat_ws(" ", t.c.headline, t.c.resume_text)
    stmt = select(
        t.c.candidate_id,
        func.ts_headline(CFG, source, tq.tsquery, bindparam(None, HEADLINE_OPTS, type_=SAText())),
    ).where(t.c.candidate_id.in_(ids))
    out: dict[uuid.UUID, list[SnippetSegment]] = {}
    for cid, raw in await db.execute(stmt):
        segments = _segments(raw)
        out[cid] = segments if any(s.hl for s in segments) else []
    return out


def _hit(row: Any, plan: Plan, snippet: list[SnippetSegment], score: float | None) -> CandidateHit:
    expanded = plan.expanded_skills
    terms = {t.lower() for t in plan.text.terms}
    skills = []
    for sid, name in list(zip(row.skill_ids or [], row.skill_names or [], strict=False))[:6]:
        matched = sid in expanded or name.lower() in terms or any(w in terms for w in qp.lexemes(name))
        skills.append(SearchSkillOut(id=sid, name=name, matched=matched))
    skills.sort(key=lambda s: not s.matched)
    return CandidateHit(
        id=row.candidate_id,
        human_id=row.human_id,
        display_name=row.display_name,
        current_title=row.current_title,
        current_employer=row.current_employer,
        city=row.city,
        state=row.state,
        country=row.country,
        experience_years=float(row.experience_years) if row.experience_years is not None else None,
        notice_days=row.notice_days,
        available_from=row.available_from,
        skills=skills,
        snippet=snippet,
        score=score,
        owner_id=row.owner_id,
        updated_at=row.candidate_updated_at,
    )


_COLUMNS = (
    "candidate_id",
    "human_id",
    "display_name",
    "current_title",
    "current_employer",
    "city",
    "state",
    "country",
    "experience_years",
    "notice_days",
    "available_from",
    "skill_ids",
    "skill_names",
    "owner_id",
    "candidate_updated_at",
)


async def _page(
    db: AsyncSession,
    principal: Principal,
    plan: Plan,
    sort: str,
    page: int,
    page_size: int,
    semantic: str | None,
    tenant_id: uuid.UUID,
) -> tuple[list[CandidateHit], int]:
    t = _DOCS
    where = _where(t, principal, plan)
    total = int((await db.execute(select(func.count()).select_from(t).where(*where))).scalar_one())
    rank = rank_expr(t, plan.text).label("rank")
    cols = [t.c[name] for name in _COLUMNS]
    offset = (page - 1) * page_size
    rows: list[Any]
    scores: dict[uuid.UUID, float] = {}
    if semantic and sort == "relevance" and offset < SEMANTIC_WINDOW and total:
        window = list(
            await db.execute(
                select(*cols, rank).where(*where).order_by(*_order(t, sort, plan.text)).limit(SEMANTIC_WINDOW)
            )
        )
        from app.services.ai import embedding_store

        cos = dict(
            await embedding_store.search_text(
                db, tenant_id, semantic, [r.candidate_id for r in window], len(window)
            )
        )
        max_cos = max((max(0.0, v) for v in cos.values()), default=0.0)
        max_rank = max((float(r.rank) for r in window), default=0.0)
        for r in window:
            fts_n = float(r.rank) / max_rank if max_rank > 0 else 0.0
            if r.candidate_id in cos and max_cos > 0:
                scores[r.candidate_id] = 0.6 * (max(0.0, cos[r.candidate_id]) / max_cos) + 0.4 * fts_n
            else:
                scores[r.candidate_id] = fts_n
        window.sort(
            key=lambda r: (-scores[r.candidate_id], -r.candidate_updated_at.timestamp(), str(r.candidate_id))
        )
        rows = window[offset : offset + page_size]
    else:
        rows = list(
            await db.execute(
                select(*cols, rank)
                .where(*where)
                .order_by(*_order(t, sort, plan.text))
                .limit(page_size)
                .offset(offset)
            )
        )
        scores = (
            {r.candidate_id: round(float(r.rank), 4) for r in rows} if plan.text.tsquery is not None else {}
        )
    snippets = await _snippets(db, [r.candidate_id for r in rows], plan.text)
    items = [
        _hit(
            r,
            plan,
            snippets.get(r.candidate_id, []),
            round(scores[r.candidate_id], 4) if r.candidate_id in scores else None,
        )
        for r in rows
    ]
    return items, total


# ================================================================ planning
async def _skill_sets(
    ctx: RequestContext | None, db: AsyncSession, skills: list[uuid.UUID], *, expand: bool = True
) -> list[set[uuid.UUID]]:
    """Per filter skill: the skill itself plus (``expand``) every skill that satisfies it -- the P1
    descendants
    (compatibility floor) and, Epic 37, ``skills_service.expand`` at depth 2 above the tenant's ``min_weight``
    (search is a filter, so every satisfying skill counts)."""
    if not skills:
        return []
    unique = list(dict.fromkeys(skills))
    if not expand:
        return [{sid} for sid in unique]
    graph = await candidates.skill_graph(db)
    sets = [{sid} | graph.descendants_of(sid) for sid in unique]
    from app.core.skill_graph_port import skill_graph

    skills_service = skill_graph()
    if ctx is None or skills_service is None:
        return sets
    cfg = await skills_service.matching_settings(db)
    keys = await skills_service.keys_for_ids(db, unique)
    by_root: dict[uuid.UUID, set[uuid.UUID]] = {}
    for row in await skills_service.expand(ctx, db, keys, depth=2, min_weight=cfg.min_weight):
        by_root.setdefault(row.root.id, set()).add(row.key.id)
    return [s | by_root.get(sid, set()) for s, sid in zip(sets, unique, strict=True)]


async def _comp_range(db: AsyncSession, f: CandidateSearchFilters) -> tuple[Decimal | None, Decimal | None]:
    if f.comp_min is None and f.comp_max is None:
        return None, None
    fx = (await matching_settings.effective(db)).fx

    def _inr(v: Decimal | None) -> Decimal | None:
        if v is None:
            return None
        return convert(annualise(v, "annual") or Decimal(0), f.comp_currency, "INR", fx)

    return _inr(f.comp_min), _inr(f.comp_max)


async def _plan(
    db: AsyncSession, filters: CandidateSearchFilters, tq: TextQuery, ctx: RequestContext | None = None
) -> Plan:
    return Plan(
        filters=filters,
        text=tq,
        skill_sets=await _skill_sets(ctx, db, list(filters.skills), expand=filters.expand_skills),
        comp_range_inr=await _comp_range(db, filters),
    )


def _check_comp(principal: Principal, f: CandidateSearchFilters) -> None:
    if (f.comp_min is not None or f.comp_max is not None) and not can_view_compensation(principal):
        raise Forbidden(
            "You may not filter by compensation", code="forbidden_filter", details={"filter": "compensation"}
        )


# ================================================================ NL interpretation
def _phrase(text_: str) -> str:
    cleaned = " ".join(text_.replace('"', " ").split())
    return f'"{cleaned}"' if cleaned else ""


def _valid_keywords(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    try:
        qp.parse(raw)
    except qp.QuerySyntaxError:
        return None
    return raw.strip()


async def interpret(
    ctx: RequestContext, db: AsyncSession, principal: Principal, data: NLInterpretation
) -> tuple[str | None, CandidateSearchFilters, str | None, list[InterpretationChip]]:
    """Map the AI interpretation to ``(q, filters, semantic_text, chips)``.

    Unresolved skills become keyword atoms.
    """
    chips: list[InterpretationChip] = []
    keyword_parts: list[str] = []
    keywords = _valid_keywords(data.keywords)
    if keywords:
        keyword_parts.append(f"({keywords})")
        chips.append(InterpretationChip(key="keywords", label="Keywords", value=keywords))
    resolved_all = await candidates.resolve_skills(ctx, db, data.skills_all)
    resolved_any = await candidates.resolve_skills(ctx, db, data.skills_any)
    names = await candidates.skill_names(
        db, {v for v in [*resolved_all.values(), *resolved_any.values()] if v}
    )
    skills: list[uuid.UUID] = []
    mode = "any"
    if data.skills_all:
        mode = "all"
        skills = list(dict.fromkeys(v for v in resolved_all.values() if v))
        for name, sid in resolved_all.items():
            if sid is None and (p := _phrase(name)):
                keyword_parts.append(p)
        chips.append(
            InterpretationChip(
                key="skills_all",
                label="Skills (all)",
                value=[
                    {"id": str(sid) if sid else None, "name": names.get(sid, n) if sid else n}
                    for n, sid in resolved_all.items()
                ],
            )
        )
    if data.skills_any:
        any_ids = [v for v in resolved_any.values() if v]
        if not data.skills_all and len(any_ids) == len(resolved_any):
            skills = list(dict.fromkeys(any_ids))
        else:
            alts = [_phrase(names.get(sid, n) if sid else n) for n, sid in resolved_any.items()]
            alts = [a for a in alts if a]
            if alts:
                keyword_parts.append("(" + " OR ".join(alts) + ")")
        chips.append(
            InterpretationChip(
                key="skills_any",
                label="Skills (any)",
                value=[
                    {"id": str(sid) if sid else None, "name": names.get(sid, n) if sid else n}
                    for n, sid in resolved_any.items()
                ],
            )
        )
    f = CandidateSearchFilters(skills=skills, skills_mode=mode)  # type: ignore[arg-type]
    if data.experience_min is not None:
        f.experience_min = Decimal(str(data.experience_min))
        chips.append(
            InterpretationChip(key="experience_min", label="Min experience (yrs)", value=data.experience_min)
        )
    if data.experience_max is not None:
        f.experience_max = Decimal(str(data.experience_max))
        chips.append(
            InterpretationChip(key="experience_max", label="Max experience (yrs)", value=data.experience_max)
        )
    if data.countries:
        f.countries = list(data.countries)
        chips.append(InterpretationChip(key="countries", label="Country", value=list(data.countries)))
    if data.cities:
        f.cities = list(data.cities)
        chips.append(InterpretationChip(key="cities", label="City", value=list(data.cities)))
    if data.work_auth:
        f.work_auth = list(data.work_auth)
        chips.append(
            InterpretationChip(key="work_auth", label="Work authorization", value=list(data.work_auth))
        )
    if data.notice_max_days is not None:
        f.notice_max_days = data.notice_max_days
        chips.append(
            InterpretationChip(key="notice_max_days", label="Max notice (days)", value=data.notice_max_days)
        )
    if data.available_by is not None:
        f.available_by = data.available_by
        chips.append(
            InterpretationChip(key="available_by", label="Available by", value=data.available_by.isoformat())
        )
    if data.comp_max is not None and can_view_compensation(principal):
        f.comp_max = Decimal(str(data.comp_max.amount))
        f.comp_currency = data.comp_max.currency.upper()
        chips.append(
            InterpretationChip(
                key="comp_max",
                label="Max compensation",
                value={"amount": f"{f.comp_max:.2f}", "currency": f.comp_currency},
            )
        )
    if data.domains:
        chips.append(InterpretationChip(key="domains", label="Domain", value=list(data.domains)))
    semantic = (data.semantic_text or "").strip()
    if data.domains and semantic:
        semantic = f"{semantic} Domains: {', '.join(data.domains)}"[:300]
    q = " AND ".join(keyword_parts) if keyword_parts else None
    if q is not None and _valid_keywords(q) is None:
        q = keywords
    return q, f, semantic or None, chips


# ================================================================ entry point
async def search_candidates(
    ctx: RequestContext, db: AsyncSession, body: CandidateSearchIn
) -> CandidateSearchOut:
    principal = _principal(ctx)
    tenant_id = ctx.tenant_id or principal.tenant_id
    assert tenant_id is not None
    _check_comp(principal, body.filters)
    notice: str | None = None
    ai_action_id: uuid.UUID | None = None
    chips: list[InterpretationChip] | None = None
    applied_q: str | None = body.q
    applied_filters: CandidateSearchFilters | None = None
    semantic = body.semantic_text or None

    if body.mode == "nl" and (body.q or "").strip():
        from app.services import ai

        result = await ai.run(
            ctx,
            db,
            "nl_search",
            "nl_search_filters.v1",
            inputs={"today": datetime.now(UTC).date().isoformat(), "tenant_currency": "INR"},
            untrusted={"query": (body.q or "")[: qp.MAX_LENGTH]},
            output_schema=NLInterpretation,
            input_ref="nl_search",
        )
        ai_action_id = result.action_id
        if result.ok and isinstance(result.data, NLInterpretation):
            applied_q, nl_filters, nl_semantic, chips = await interpret(ctx, db, principal, result.data)
            # Explicit filters from the request win over interpreted ones.
            merged = nl_filters.model_copy(update=body.filters.model_dump(exclude_defaults=True))
            applied_filters = merged
            semantic = nl_semantic or (body.q or "").strip()[:300]
            tq = text_query(applied_q)
        else:
            notice = "ai_unavailable"
            applied_filters = body.filters
            tq = text_query(body.q, websearch=True)
            semantic = None
    else:
        applied_filters = body.filters
        tq = text_query(body.q)

    plan = await _plan(db, applied_filters, tq, ctx)
    items, total = await _page(db, principal, plan, body.sort, body.page, body.page_size, semantic, tenant_id)
    facets, approximate = await compute_facets(db, principal, plan, total)
    return CandidateSearchOut(
        items=items,
        total=total,
        page=body.page,
        page_size=body.page_size,
        facets=facets,
        facets_approximate=approximate,
        interpretation=chips,
        notice=notice,  # type: ignore[arg-type]
        ai_action_id=ai_action_id,
        applied_q=applied_q,
        applied_filters=applied_filters if body.mode == "nl" else None,
        semantic_text=semantic,
    )
