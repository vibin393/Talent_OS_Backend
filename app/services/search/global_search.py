"""Header global search (Epic 5, story 5.6; FR-73).

Classifier: a human id (``CAN-000123``, ``REQ-1056``; any tenant prefix) -> exact id lookup pinned as
``exact``; an email -> exact (citext) match; a phone (>= 7 digits) -> E.164 exact match; anything else ->
name / title trigram + prefix. Fans out sequentially to each domain's quick search (candidates from the search
projection, ``demands.matching_port.quick_search``, ``clients_service.quick_search`` for clients + contacts),
each applying its own permission + scope. A group is omitted when the user lacks ``<resource>:read``; every
group runs under a 250 ms ``statement_timeout`` (savepoint) and reports ``timed_out`` instead of failing.
"""

import dataclasses
import re
from collections.abc import Awaitable, Callable

from loguru import logger
from sqlalchemy import Text as SAText
from sqlalchemy import bindparam, func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal, scope_condition
from app.models.search import CandidateSearchDoc
from app.schemas.search import GlobalGroup, GlobalSearchOut, QuickHit
from app.services.candidates.common import normalize_email, normalize_phone

GROUP_TIMEOUT_MS = 250
MAX_LIMIT = 20
HUMAN_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,9}-\d+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[\d\s().-]+$")
NAME_SIMILARITY = 0.3


def classify(q: str) -> str:
    term = q.strip()
    if HUMAN_ID_RE.match(term):
        return "human_id"
    if EMAIL_RE.match(term):
        return "email"
    if PHONE_RE.match(term) and len(re.sub(r"\D", "", term)) >= 7:
        return "phone"
    return "text"


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _candidates(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[QuickHit]:
    principal = ctx.principal
    assert isinstance(principal, Principal)
    d = CandidateSearchDoc
    kind = classify(q)
    term = q.strip()
    stmt = select(d).where(d.is_archived.is_(False))
    scope = scope_condition(principal, "candidate:read", d.owner_id)
    if scope is not None:
        stmt = stmt.where(scope)
    if kind == "human_id":
        stmt = stmt.where(func.upper(d.human_id) == term.upper())
    elif kind == "email":
        stmt = stmt.where(d.email == (normalize_email(term) or term.lower()))
    elif kind == "phone":
        phone = normalize_phone(term)
        if phone is None:
            return []
        stmt = stmt.where(d.phone_e164 == phone)
    else:
        qn = func.f_unaccent(func.lower(bindparam("gq", term, type_=SAText())))
        stmt = stmt.where(
            or_(
                d.name_norm.op("%")(qn),
                d.name_norm.like(func.concat(func.f_unaccent(_escape(term.lower())), "%")),
                func.upper(d.human_id).like(_escape(term.upper()) + "%"),
            )
        ).order_by(func.similarity(d.name_norm, qn).desc(), d.candidate_updated_at.desc())
    rows = (await db.execute(stmt.order_by(d.candidate_id).limit(limit))).scalars()
    hits = []
    for r in rows:
        sub = " · ".join(p for p in (r.human_id, r.email or r.current_title) if p)
        hits.append(
            QuickHit(
                id=r.candidate_id,
                type="candidate",
                title=r.display_name,
                subtitle=sub,
                human_id=r.human_id,
                url=f"/candidates/{r.candidate_id}",
            )
        )
    return hits


async def _demands(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[QuickHit]:
    from app.services.demands import matching_port

    rows = await matching_port.quick_search(ctx, db, q, limit)
    return [
        QuickHit(
            id=r["id"],
            type="demand",
            title=r["title"],
            subtitle=r["subtitle"],
            human_id=r["human_id"],
            url=f"/demands/{r['id']}",
        )
        for r in rows
    ]


async def _clients_and_contacts(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[QuickHit]:
    from app.services import clients_service

    hits = await clients_service.quick_search(ctx, db, q, limit)
    out: list[QuickHit] = []
    for h in hits:
        if h.kind == "client":
            out.append(
                QuickHit(id=h.id, type="client", title=h.title, subtitle=h.subtitle, url=f"/clients/{h.id}")
            )
        else:
            out.append(
                QuickHit(
                    id=h.id,
                    type="contact",
                    title=h.title,
                    subtitle=h.subtitle,
                    url=f"/clients/{h.client_id}?tab=contacts&contact={h.id}",
                )
            )
    return out


async def _timed(
    db: AsyncSession, work: Callable[[], Awaitable[list[QuickHit]]]
) -> tuple[list[QuickHit], bool]:
    previous = (await db.execute(text("SELECT current_setting('statement_timeout')"))).scalar_one()
    try:
        async with db.begin_nested():
            await db.execute(
                text("SELECT set_config('statement_timeout', :v, true)"), {"v": str(GROUP_TIMEOUT_MS)}
            )
            hits = await work()
            await db.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": previous})
        return hits, False
    except DBAPIError as exc:
        message = str(exc).lower()
        if "statement timeout" not in message and "canceling statement" not in message:
            raise
        logger.warning("Global search group timed out after {} ms", GROUP_TIMEOUT_MS)
        await db.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": previous})
        return [], True


def _group(kind: str, hits: list[QuickHit], limit: int, timed_out: bool) -> GlobalGroup:
    mine = [h for h in hits if h.type == kind]
    return GlobalGroup(type=kind, items=mine[:limit], has_more=len(mine) > limit, timed_out=timed_out)  # type: ignore[arg-type]


# ================================================================ Epic 34 (A5): group registry
QuickSearchFn = Callable[[RequestContext, AsyncSession, str, str, int], Awaitable[list[QuickHit]]]


@dataclasses.dataclass(frozen=True, slots=True)
class GlobalGroupDef:
    key: str
    label: str
    permission: str | tuple[str, ...]  # Epic 65: a tuple = any one of them
    quick_search: QuickSearchFn  # (ctx, db, q, classified, limit) -> hits; ``exact=True`` hits pin first
    exact: QuickSearchFn | None = None


GLOBAL_GROUPS: dict[str, GlobalGroupDef] = {}


def register_global_group(
    key: str,
    label: str,
    permission: str | tuple[str, ...],
    quick_search: QuickSearchFn,
    exact: QuickSearchFn | None = None,
) -> None:
    """Register a header search group (Phase 2 domains, through reporting's read-only projections)."""
    GLOBAL_GROUPS[key] = GlobalGroupDef(key, label, permission, quick_search, exact)


async def global_search(ctx: RequestContext, db: AsyncSession, q: str, limit: int = 5) -> GlobalSearchOut:
    principal = ctx.principal
    assert isinstance(principal, Principal)
    limit = max(1, min(limit, MAX_LIMIT))
    term = (q or "").strip()[:200]
    if len(term) < 2:
        return GlobalSearchOut(exact=None, groups=[])
    groups: list[GlobalGroup] = []
    kind = classify(term)
    exact: QuickHit | None = None

    if principal.has("candidate:read"):
        hits, timed_out = await _timed(db, lambda: _candidates(ctx, db, term, limit + 1))
        groups.append(_group("candidate", hits, limit, timed_out))
        if kind in {"human_id", "email", "phone"} and hits:
            exact = hits[0]
    if principal.has("demand:read"):
        hits, timed_out = await _timed(db, lambda: _demands(ctx, db, term, limit + 1))
        groups.append(_group("demand", hits, limit, timed_out))
        if exact is None and kind == "human_id":
            exact = next((h for h in hits if (h.human_id or "").upper() == term.upper()), None)
    if principal.has("client:read") or principal.has("contact:read"):
        hits, timed_out = await _timed(db, lambda: _clients_and_contacts(ctx, db, term, limit + 1))
        if principal.has("client:read"):
            groups.append(_group("client", hits, limit, timed_out))
        if principal.has("contact:read"):
            groups.append(_group("contact", hits, limit, timed_out))
            if exact is None and kind == "email":
                exact = next((h for h in hits if h.type == "contact"), None)
    registry_exact: QuickHit | None = None
    for group in list(GLOBAL_GROUPS.values()):
        needed = group.permission if isinstance(group.permission, tuple) else (group.permission,)
        if not any(principal.has(p) for p in needed):
            continue
        hits, timed_out = await _timed(db, lambda g=group: g.quick_search(ctx, db, term, kind, limit + 1))  # type: ignore[misc]
        groups.append(_group(group.key, hits, limit, timed_out))
        if registry_exact is None:
            registry_exact = next((h for h in hits if h.exact), None)
    # FR-178: an exact Invoice number / Placement id is pinned above every group.
    if registry_exact is not None:
        exact = registry_exact
    return GlobalSearchOut(exact=exact, groups=groups)
