# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Internal openings (Epic 41 A2, FR-209): the ``internal_openings`` projection, eligibility rules per Demand and the
Employee board.

Projection (no client name, no rates / compensation, no hiring-manager contact): a Demand that is ``open``, not
archived and ``visibility IN ('internal', 'both')`` has an ``open`` row; otherwise its row is ``closed``. Kept in sync
by after-commit handlers (``hooks.py`` -> ``tasks.mobility.sync_opening``), a nightly ``mobility.reconcile_openings``
and a cheap ``refresh`` before every board read (visibility edits on an open Demand emit no P1 event).

The board lists open openings the Employee is eligible for (``include_ineligible`` adds the others with reasons),
ordered by stored fit (``fit_pct`` desc, nulls last) then ``opened_at`` desc.
"""

import uuid
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.mobility import LIVE_STATUSES, InternalOpening, InternalOpeningRule, MobilityApplication
from app.schemas.mobility import (
    OpeningDetailOut,
    OpeningOut,
    OpeningPage,
    OpeningRulesIn,
    OpeningRulesOut,
    OpeningSkill,
)
from app.services.mobility import eligibility, matches
from app.services.mobility.common import MANAGE, not_found, now, require, settings_row, tenant_of, today
from app.services.mobility.employees import Employee

SUMMARY_MAX = 4096
SUMMARY_SOURCE_MAX = 2500


# ================================================================ projection
def _qualifies(d: Any) -> bool:
    return d.status == "open" and d.archived_at is None and d.visibility in ("internal", "both")


async def _summary(db: AsyncSession, d: Any) -> str:
    from app.models.demands import JdVersion
    from app.services.portal.common import render_markdown

    version = await db.get(JdVersion, d.published_jd_version_id) if d.published_jd_version_id else None
    if version is None:
        version = (
            await db.execute(
                select(JdVersion).where(JdVersion.demand_id == d.id).order_by(JdVersion.version_no.desc()).limit(1)
            )
        ).scalar_one_or_none()
    source = (version.content if version else "") or ""
    if len(source) > SUMMARY_SOURCE_MAX:
        cut = source[:SUMMARY_SOURCE_MAX]
        source = cut[: cut.rfind("\n")] if "\n" in cut else cut
        source = source.rstrip() + "\n\n..."
    html = render_markdown(source)
    while len(html) > SUMMARY_MAX and source:
        source = source[: int(len(source) * 0.8)]
        html = render_markdown(source + "\n\n...")
    return html if len(html) <= SUMMARY_MAX else ""


async def _skills(db: AsyncSession, demand_id: uuid.UUID) -> list[dict[str, Any]]:
    from app.models.demands import DemandSkill

    rows = (
        await db.execute(
            select(DemandSkill.skill_label, DemandSkill.min_proficiency, DemandSkill.kind)
            .where(DemandSkill.demand_id == demand_id)
            .order_by(DemandSkill.kind, DemandSkill.position, DemandSkill.skill_label)
        )
    ).all()
    return [{"name": label, "min_proficiency": prof} for label, prof, _ in rows]


async def sync_demand(db: AsyncSession, demand_id: uuid.UUID) -> str:
    """Upsert / close one Demand's projection row; returns ``open`` | ``closed`` | ``absent``."""
    from app.models.demands import Demand

    d = await db.get(Demand, demand_id)
    row = await db.get(InternalOpening, demand_id)
    if d is None or not _qualifies(d):
        if row is not None and row.status != "closed":
            row.status = "closed"
            row.updated_at = func.now()  # DB clock (compared with demands.updated_at)
            await db.flush()
        return "closed" if row is not None else "absent"
    values = {
        "title": d.title,
        "location_text": d.location,
        "work_mode": d.work_mode,
        "employment_type": d.employment_type,
        "summary_html": await _summary(db, d),
        "skills": await _skills(db, d.id),
        "visibility": d.visibility,
        "opened_at": d.opened_at or now(),
        "closes_at": datetime.combine(d.target_closure_date, time.min, tzinfo=UTC) if d.target_closure_date else None,
        "status": "open",
    }
    if row is None:
        row = InternalOpening(tenant_id=d.tenant_id, demand_id=d.id, **values)
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    row.updated_at = func.now()  # DB clock (compared with demands.updated_at)
    await db.flush()
    return "open"


async def refresh(db: AsyncSession) -> int:
    """Repair drift cheaply: qualifying Demands without an up-to-date open row, and open rows whose Demand stopped
    qualifying. Returns the number of rows touched."""
    from app.models.demands import Demand

    stale = (
        await db.execute(
            select(Demand.id)
            .outerjoin(InternalOpening, InternalOpening.demand_id == Demand.id)
            .where(
                Demand.status == "open",
                Demand.archived_at.is_(None),
                Demand.visibility.in_(("internal", "both")),
                or_(
                    InternalOpening.demand_id.is_(None),
                    InternalOpening.status != "open",
                    InternalOpening.updated_at < Demand.updated_at,
                ),
            )
        )
    ).scalars()
    gone = (
        await db.execute(
            select(InternalOpening.demand_id)
            .join(Demand, Demand.id == InternalOpening.demand_id)
            .where(
                InternalOpening.status == "open",
                or_(Demand.status != "open", Demand.archived_at.is_not(None), Demand.visibility == "external"),
            )
        )
    ).scalars()
    ids = list(dict.fromkeys([*stale, *gone]))
    for did in ids:
        await sync_demand(db, did)
    return len(ids)


async def reconcile(db: AsyncSession) -> int:
    """Nightly: re-sync every open row and every qualifying Demand."""
    from app.models.demands import Demand

    ids = set(
        (
            await db.execute(
                select(Demand.id).where(
                    or_(
                        and_(
                            Demand.status == "open",
                            Demand.archived_at.is_(None),
                            Demand.visibility.in_(("internal", "both")),
                        ),
                        Demand.id.in_(select(InternalOpening.demand_id).where(InternalOpening.status == "open")),
                    )
                )
            )
        ).scalars()
    )
    for did in sorted(ids, key=str):
        await sync_demand(db, did)
    return len(ids)


# ================================================================ rules (mobility:manage)
async def _demand_for_rules(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> Any:
    from app.services.demands import matching_port as demands

    require(ctx, MANAGE)
    return await demands.get_demand(ctx, db, demand_id)  # 404 outside the caller's demand scope


def _rules_out(d: Any, row: InternalOpeningRule | None, on_board: bool) -> OpeningRulesOut:
    return OpeningRulesOut(
        demand_id=d.id,
        visibility=d.visibility,
        min_tenure_months=row.min_tenure_months if row else None,
        grades=list(row.grades) if row and row.grades else None,
        locations=list(row.locations) if row and row.locations else None,
        business_unit_ids=list(row.business_unit_ids) if row and row.business_unit_ids else None,
        note=row.note if row else None,
        on_board=on_board,
    )


async def _on_board(db: AsyncSession, demand_id: uuid.UUID) -> bool:
    row = await db.get(InternalOpening, demand_id)
    return row is not None and row.status == "open"


async def get_rules(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> OpeningRulesOut:
    d = await _demand_for_rules(ctx, db, demand_id)
    await sync_demand(db, demand_id)
    return _rules_out(d, await db.get(InternalOpeningRule, demand_id), await _on_board(db, demand_id))


async def put_rules(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: OpeningRulesIn) -> OpeningRulesOut:
    d = await _demand_for_rules(ctx, db, demand_id)
    row = await db.get(InternalOpeningRule, demand_id)
    if row is None:
        row = InternalOpeningRule(tenant_id=tenant_of(db), demand_id=demand_id)
        db.add(row)
    row.min_tenure_months = data.min_tenure_months
    row.grades = list(dict.fromkeys(data.grades or [])) or None
    row.locations = list(dict.fromkeys(data.locations or [])) or None
    row.business_unit_ids = list(dict.fromkeys(data.business_unit_ids or [])) or None
    row.note = (data.note or "").strip() or None
    await db.flush()
    await sync_demand(db, demand_id)
    return _rules_out(d, row, await _on_board(db, demand_id))


# ================================================================ Employee board
async def load_openings(
    db: AsyncSession, *, q: str | None = None, location: str | None = None, demand_id: uuid.UUID | None = None
) -> list[InternalOpening]:
    """Open projection rows (works in an internal or a Worker session: the table's org policy admits both)."""
    stmt = select(InternalOpening).where(InternalOpening.status == "open")
    if demand_id is not None:
        stmt = stmt.where(InternalOpening.demand_id == demand_id)
    if q and q.strip():
        stmt = stmt.where(InternalOpening.title.ilike(f"%{q.strip()}%"))
    if location and location.strip():
        stmt = stmt.where(InternalOpening.location_text.ilike(f"%{location.strip()}%"))
    return list((await db.execute(stmt)).scalars())


async def own_applications(db: AsyncSession, emp: Employee) -> dict[uuid.UUID, MobilityApplication]:
    """The Employee's latest internal application per Demand."""
    cond = []
    if emp.candidate_id is not None:
        cond.append(MobilityApplication.candidate_id == emp.candidate_id)
    if emp.user_id is not None:
        cond.append(MobilityApplication.user_id == emp.user_id)
    if not cond:
        return {}
    rows = (
        await db.execute(select(MobilityApplication).where(or_(*cond)).order_by(MobilityApplication.submitted_at))
    ).scalars()
    return {r.demand_id: r for r in rows}


def _out(row: InternalOpening, result: eligibility.EligibilityResult, fit: matches.Fit | None, app: MobilityApplication | None) -> OpeningOut:
    return OpeningOut(
        demand_id=row.demand_id,
        title=row.title,
        location_text=row.location_text,
        work_mode=row.work_mode,
        employment_type=row.employment_type,
        skills=[OpeningSkill(name=str(s.get("name")), min_proficiency=s.get("min_proficiency")) for s in row.skills or []],
        visibility=row.visibility,  # type: ignore[arg-type]
        opened_at=row.opened_at,
        closes_at=row.closes_at,
        eligible=result.eligible,
        reasons=list(result.reasons),
        fit_pct=fit.fit_pct if fit else None,
        fit_reasons=list(fit.reasons) if fit else [],
        application_id=app.id if app else None,
        application_status=app.status if app else None,  # type: ignore[arg-type]
    )


async def decorate(db: AsyncSession, emp: Employee, rows: list[InternalOpening]) -> list[tuple[OpeningOut, InternalOpening]]:
    settings = await settings_row(db)
    ids = [r.demand_id for r in rows]
    rules = await eligibility.load_rules(db, ids)
    fits = await matches.fit_for_employee(None, db, emp.candidate_id, ids)
    apps = await own_applications(db, emp)
    on = today()
    out = []
    for r in rows:
        result = eligibility.evaluate(rules.get(r.demand_id), settings.min_tenure_default_months, emp, on)
        out.append((_out(r, result, fits.get(r.demand_id), apps.get(r.demand_id)), r))
    return out


def _sort_key(o: OpeningOut) -> tuple[Any, ...]:
    return (o.fit_pct is None, -(o.fit_pct or 0), -o.opened_at.timestamp(), str(o.demand_id))


def page_of(
    items: list[OpeningOut], *, include_ineligible: bool, page: int, page_size: int, board_enabled: bool
) -> OpeningPage:
    shown = [o for o in items if include_ineligible or o.eligible]
    shown.sort(key=_sort_key)
    start = (page - 1) * page_size
    return OpeningPage(
        items=shown[start : start + page_size], total=len(shown), page=page, page_size=page_size, board_enabled=board_enabled
    )


def can_apply(o: OpeningOut) -> bool:
    return o.eligible and (o.application_status is None or o.application_status not in (*LIVE_STATUSES, "transfer_confirmed"))


def detail_of(o: OpeningOut, row: InternalOpening, disclosure: str) -> OpeningDetailOut:
    return OpeningDetailOut(**o.model_dump(), summary_html=row.summary_html, disclosure_text=disclosure, can_apply=can_apply(o))


async def board_for(
    db: AsyncSession,
    emp: Employee,
    *,
    q: str | None,
    location: str | None,
    include_ineligible: bool,
    page: int,
    page_size: int,
    enabled: bool,
) -> OpeningPage:
    if not enabled:
        return OpeningPage(items=[], total=0, page=page, page_size=page_size, board_enabled=False)
    await refresh(db)
    decorated = await decorate(db, emp, await load_openings(db, q=q, location=location))
    return page_of([o for o, _ in decorated], include_ineligible=include_ineligible, page=page, page_size=page_size, board_enabled=True)


async def detail_for(db: AsyncSession, emp: Employee, demand_id: uuid.UUID, *, enabled: bool) -> OpeningDetailOut:
    if not enabled:
        raise not_found("Opening")
    await refresh(db)
    rows = await load_openings(db, demand_id=demand_id)
    if not rows:
        raise not_found("Opening")
    (o, row), = await decorate(db, emp, rows)
    return detail_of(o, row, (await settings_row(db)).disclosure_text)
