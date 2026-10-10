"""Thin read functions the demands service offers to matching / search (Epic 5), plus the
``DemandRequirementsChanged`` event.

* ``get_demand(ctx, db, id)`` -- visibility check (404 when out of the caller's scope).
* ``get_matching_profile(db, id)`` -- ``DemandMatchProfile`` (structured requirements only).
* ``list_open_demand_ids(db)``, ``get_assignees(db, id)``, ``demand_status(db, id)``, ``labels(db, ids)``.
* ``quick_search(ctx, db, q, limit)`` -- header global search (exact ``req_id`` first, then title).

``DemandRequirementsChanged`` is emitted by a flush listener whenever a matching-relevant demand column or a
demand skill row is written, so every write path (edit, AI intake confirm, JD generation...) is covered.
"""

import re
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Select, column, event, func, inspect, or_, select, table
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction, UOWTransaction

from app.core.context import RequestContext
from app.core.events import emit
from app.core.permissions import Principal
from app.events.demands import DemandRequirementsChanged
from app.models.demands import Demand, DemandAssignment, DemandSkill, JdVersion
from app.schemas.matching_profiles import DemandMatchProfile, RequiredSkill, annualise
from app.services.demands.common import READ, access_condition, get_for

MATCH_FIELDS = frozenset(
    {
        "title",
        "country",
        "location",
        "work_mode",
        "min_experience_years",
        "max_experience_years",
        "certifications",
        "work_authorization",
        "start_date",
        "pay_rate_max",
        "currency",
        "rate_unit",
        "department",
    }
)
SUMMARY_KINDS = ("recruiter_summary", "short")
SUMMARY_MAX = 500

_team_members = table("team_members", column("team_id"), column("user_id"))
_SEEN_KEY = "epic5_demand_requirement_events"
_registered = False


# ================================================================ reads
async def get_demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> Demand:
    """Return the demand if the caller may read it (404 otherwise, incl. other tenants)."""
    return await get_for(ctx, db, demand_id, READ, for_update=False)


def _split_location(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    parts = [p.strip() for p in re.split(r"[,/|]", text) if p.strip()]
    city = parts[0] if parts else None
    state = parts[1] if len(parts) > 2 else (parts[1] if len(parts) == 2 else None)
    return city, state


async def get_matching_profile(db: AsyncSession, demand_id: uuid.UUID) -> DemandMatchProfile | None:
    demand = await db.get(Demand, demand_id)
    if demand is None:
        return None
    skills = list(
        (
            await db.execute(
                select(DemandSkill).where(DemandSkill.demand_id == demand_id).order_by(DemandSkill.position)
            )
        ).scalars()
    )
    summary = (
        await db.execute(
            select(JdVersion.content)
            .where(JdVersion.demand_id == demand_id, JdVersion.kind.in_(SUMMARY_KINDS))
            .order_by(JdVersion.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    city, state = _split_location(demand.location)
    return DemandMatchProfile(
        demand_id=demand.id,
        title=demand.title,
        skills=tuple(
            RequiredSkill(
                skill_id=s.skill_id,
                label=s.skill_label,
                mandatory=s.kind == "mandatory",
                min_years=s.min_years,
                # Epic 37 (FR-190): minimum proficiency, expansion flag and the skill key kind.
                min_proficiency=s.min_proficiency,
                expand=s.expand,
                skill_ref_kind=s.skill_ref_kind,
            )
            for s in skills
        ),
        experience_min=demand.min_experience_years,
        experience_max=demand.max_experience_years,
        remote=demand.work_mode == "remote",
        city=city,
        state=state,
        country=demand.country,
        required_work_auth=tuple(demand.work_authorization or ()),
        budget_max_annual=annualise(demand.pay_rate_max, demand.rate_unit),
        currency=demand.currency,
        needed_by=demand.start_date,
        domains=(),
        summary=(summary or "")[:SUMMARY_MAX] or None,
    )


async def list_open_demand_ids(db: AsyncSession) -> list[uuid.UUID]:
    stmt = select(Demand.id).where(Demand.status == "open", Demand.archived_at.is_(None)).order_by(Demand.id)
    return [r[0] for r in await db.execute(stmt)]


async def demand_status(db: AsyncSession, demand_id: uuid.UUID) -> str | None:
    return (await db.execute(select(Demand.status).where(Demand.id == demand_id))).scalar_one_or_none()


async def get_assignees(db: AsyncSession, demand_id: uuid.UUID) -> list[uuid.UUID]:
    """Active assignee user ids (direct + members of assigned teams), primary first."""
    rows = list(
        (
            await db.execute(
                select(DemandAssignment)
                .where(DemandAssignment.demand_id == demand_id, DemandAssignment.removed_at.is_(None))
                .order_by(DemandAssignment.is_primary.desc(), DemandAssignment.assigned_at)
            )
        ).scalars()
    )
    out: list[uuid.UUID] = [r.user_id for r in rows if r.user_id is not None]
    team_ids = [r.team_id for r in rows if r.team_id is not None]
    if team_ids:
        members = await db.execute(
            select(_team_members.c.user_id).where(_team_members.c.team_id.in_(team_ids))
        )
        out.extend(m[0] for m in members)
    return list(dict.fromkeys(out))


async def labels(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    wanted = set(ids)
    if not wanted:
        return {}
    rows = await db.execute(
        select(Demand.id, Demand.req_id, Demand.title, Demand.status).where(Demand.id.in_(wanted))
    )
    return {r[0]: {"req_id": r[1], "title": r[2], "status": r[3]} for r in rows}


# ================================================================ global search
_REQ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,9}-\d+$")


def _visible(principal: Principal) -> Select[tuple[Demand]]:
    stmt = select(Demand).where(Demand.archived_at.is_(None))
    cond = access_condition(principal, READ)
    return stmt if cond is None else stmt.where(cond)


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def quick_search(ctx: RequestContext, db: AsyncSession, q: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return visible demands matching ``q``: exact ``req_id`` first, then title trigram / prefix."""
    principal = ctx.principal
    if not isinstance(principal, Principal) or not principal.has(READ):
        return []
    term = q.strip()
    if not term:
        return []
    hits: list[Demand] = []
    if _REQ_RE.match(term):
        exact = await db.execute(
            _visible(principal).where(func.upper(Demand.req_id) == term.upper()).limit(1)
        )
        hits.extend(exact.scalars())
    if len(hits) < limit and len(term) >= 2:
        norm = term.lower()
        title = func.lower(Demand.title)
        stmt = (
            _visible(principal)
            .where(
                or_(
                    title.op("%")(norm),
                    title.like(f"%{_escape(norm)}%", escape="\\"),
                    func.upper(Demand.req_id).like(f"{_escape(term.upper())}%", escape="\\"),
                ),
                *([Demand.id.not_in([h.id for h in hits])] if hits else []),
            )
            .order_by(func.similarity(title, norm).desc(), Demand.created_at.desc())
            .limit(limit - len(hits))
        )
        hits.extend((await db.execute(stmt)).scalars())
    return [
        {
            "id": d.id,
            "human_id": d.req_id,
            "title": d.title,
            "subtitle": f"{d.req_id} · {d.status.replace('_', ' ')}",
            "exact": bool(_REQ_RE.match(term) and d.req_id.upper() == term.upper()),
        }
        for d in hits
    ]


# ================================================================ requirement change events
def _tenant(session: Session, obj: Any) -> uuid.UUID | None:
    raw = getattr(obj, "tenant_id", None) or session.info.get("tenant_id")
    return uuid.UUID(str(raw)) if raw is not None else None


def _emit(session: Session, tenant_id: uuid.UUID | None, demand_id: uuid.UUID | None) -> None:
    if tenant_id is None or demand_id is None:
        return
    seen: set[uuid.UUID] = session.info.setdefault(_SEEN_KEY, set())
    if demand_id in seen:
        return
    seen.add(demand_id)
    emit(session, DemandRequirementsChanged(tenant_id=tenant_id, demand_id=demand_id))


def _on_flush(session: Session, _ctx: UOWTransaction, _instances: object) -> None:
    for obj in list(session.dirty):
        if isinstance(obj, Demand) and session.is_modified(obj, include_collections=False):
            state = inspect(obj)
            if any(state.attrs[k].history.has_changes() for k in MATCH_FIELDS):
                _emit(session, _tenant(session, obj), obj.id)
        elif isinstance(obj, DemandSkill) and session.is_modified(obj, include_collections=False):
            _emit(session, _tenant(session, obj), obj.demand_id)
    for obj in [*session.new, *session.deleted]:
        if isinstance(obj, DemandSkill):
            _emit(session, _tenant(session, obj), obj.demand_id)


def _clear(session: Session) -> None:
    session.info.pop(_SEEN_KEY, None)


def _clear_after_rollback(session: Session, previous: SessionTransaction) -> None:
    if previous.parent is None:
        session.info.pop(_SEEN_KEY, None)


def register() -> None:
    global _registered  # idempotent process-wide wiring
    if _registered:
        return
    event.listen(Session, "before_flush", _on_flush)
    event.listen(Session, "after_commit", _clear)
    event.listen(Session, "after_soft_rollback", _clear_after_rollback)
    _registered = True


register()

__all__ = [
    "demand_status",
    "get_assignees",
    "get_demand",
    "get_matching_profile",
    "labels",
    "list_open_demand_ids",
    "quick_search",
]
