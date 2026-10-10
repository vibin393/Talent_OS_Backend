# ruff: noqa: E501
"""Report Definitions CRUD (Epic 47 story 47.2, FR-237).

* Create / edit your own (``report:build``); every definition or viz change appends a
  ``report_definition_versions`` row and bumps ``definition_version``.
* Validation = ``core.semantic.validate(defn, principal=<author>)`` + a dry ``compile`` (no execution) so errors
  are the foundation's 422 ``semantic_*`` codes with ``details.path`` (403 when the author cannot read the
  source); ``viz`` must name keys of the definition (``validate.check_viz``).
* Read = owner, or ``report:read`` + a matching share (``sharing.visible_condition``).
* Saving a definition that passes the registry check clears ``needs_repair``.
"""

import uuid
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import Principal, check
from app.core.semantic import Definition, SemanticContext, compile, get_source, validate
from app.models.report_builder import ReportDefinition, ReportDefinitionVersion, ReportPin, ReportSchedule
from app.schemas.report_builder import ReportCreate, ReportOut, ReportUpdate, VizSpec
from app.services.reporting.builder import repair
from app.services.reporting.builder.common import BUILD, principal_of
from app.services.reporting.builder.sharing import shares_out, visible_condition, visible_report
from app.services.reporting.builder.validate import check_viz

__all__ = [
    "archive",
    "create",
    "get",
    "list_reports",
    "quick_search_definitions",
    "report_out",
    "search_definitions",
    "update",
    "validated",
]


def _source_label(key: str) -> str | None:
    try:
        return get_source(key).label
    except KeyError:
        return None


async def validated(
    ctx: RequestContext, db: AsyncSession, definition: dict[str, Any], viz: VizSpec
) -> tuple[Definition, VizSpec]:
    """Author-side validation: semantic validate (author's field permissions) + dry compile + viz keys."""
    principal = principal_of(ctx)
    defn = validate(definition, principal=principal)
    await compile(defn, SemanticContext(ctx, db, "report"))  # no execution
    return defn, check_viz(viz, defn)


def _stored(defn: Definition) -> dict[str, Any]:
    return defn.model_dump(mode="json", by_alias=True, exclude_none=True)


async def report_out(db: AsyncSession, principal: Principal, rows: list[ReportDefinition]) -> list[ReportOut]:
    if not rows:
        return []
    from app.services import entity_refs

    ids = [r.id for r in rows]
    shares = await shares_out(db, ids)
    owners = await entity_refs.users(db, {r.owner_id for r in rows})
    schedules = dict(
        (
            await db.execute(
                select(ReportSchedule.report_id, func.count())
                .where(ReportSchedule.report_id.in_(ids))
                .group_by(ReportSchedule.report_id)
            )
        )
        .tuples()
        .all()
    )
    pinned = set(
        (
            await db.execute(
                select(ReportPin.report_id).where(
                    ReportPin.report_id.in_(ids), ReportPin.user_id == principal.id
                )
            )
        ).scalars()
    )
    out = []
    for r in rows:
        owner = owners.get(r.owner_id)
        out.append(
            ReportOut(
                id=r.id,
                name=r.name,
                description=r.description,
                owner_id=r.owner_id,
                owner_name=owner.name if owner else None,
                source_key=r.source_key,
                source_label=_source_label(r.source_key),
                definition=r.definition,
                definition_version=r.definition_version,
                viz=VizSpec.model_validate(r.viz),
                visibility=r.visibility,
                status=r.status,
                repair_issues=list(r.repair_issues or []),
                shares=shares.get(r.id, []),
                schedule_count=int(schedules.get(r.id, 0)),
                pinned=r.id in pinned,
                is_owner=r.owner_id == principal.id,
                last_run_at=r.last_run_at,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
        )
    return out


def _version(report: ReportDefinition, principal: Principal) -> ReportDefinitionVersion:
    return ReportDefinitionVersion(
        tenant_id=report.tenant_id,
        report_id=report.id,
        version=report.definition_version,
        definition=report.definition,
        viz=report.viz,
        created_by=principal.id,
    )


async def create(ctx: RequestContext, db: AsyncSession, data: ReportCreate) -> ReportOut:
    principal = principal_of(ctx)
    check(principal, BUILD)
    defn, viz = await validated(ctx, db, data.definition, data.viz)
    report = ReportDefinition(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        name=data.name.strip(),
        description=data.description,
        owner_id=principal.id,
        source_key=defn.source,
        definition=_stored(defn),
        definition_version=1,
        viz=viz.model_dump(exclude_none=True),
    )
    db.add(report)
    await db.flush()
    db.add(_version(report, principal))
    await db.flush()
    await db.refresh(report)
    return (await report_out(db, principal, [report]))[0]


async def _own(
    ctx: RequestContext, db: AsyncSession, report_id: uuid.UUID
) -> tuple[Principal, ReportDefinition]:
    principal = principal_of(ctx)
    check(principal, BUILD)
    report = await visible_report(db, principal, report_id, for_update=True)
    if report.owner_id != principal.id:
        raise NotFound("Report not found", code="report_not_found")
    return principal, report


async def update(
    ctx: RequestContext, db: AsyncSession, report_id: uuid.UUID, data: ReportUpdate
) -> ReportOut:
    principal, report = await _own(ctx, db, report_id)
    if data.name is not None:
        report.name = data.name.strip()
    if "description" in data.model_fields_set:
        report.description = data.description
    if data.definition is not None or data.viz is not None:
        definition = data.definition if data.definition is not None else report.definition
        viz = data.viz if data.viz is not None else VizSpec.model_validate(report.viz)
        defn, viz = await validated(ctx, db, definition, viz)
        stored = _stored(defn)
        viz_json = viz.model_dump(exclude_none=True)
        if stored != report.definition or viz_json != report.viz:
            report.definition = stored
            report.viz = viz_json
            report.source_key = defn.source
            report.definition_version += 1
            db.add(_version(report, principal))
        repair.clear(report)
    await db.flush()
    await db.refresh(report)
    return (await report_out(db, principal, [report]))[0]


async def archive(ctx: RequestContext, db: AsyncSession, report_id: uuid.UUID) -> None:
    from app.services.reporting.builder.common import now

    _, report = await _own(ctx, db, report_id)
    report.archived_at = now()
    await db.flush()


async def get(ctx: RequestContext, db: AsyncSession, report_id: uuid.UUID) -> ReportOut:
    principal = principal_of(ctx)
    report = await visible_report(db, principal, report_id)
    return (await report_out(db, principal, [report]))[0]


async def list_reports(
    ctx: RequestContext,
    db: AsyncSession,
    params: PageParams,
    *,
    tab: Literal["mine", "shared", "all"] = "all",
    q: str | None = None,
    status: str | None = None,
) -> Page[ReportOut]:
    principal = principal_of(ctx)
    stmt = select(ReportDefinition).where(
        ReportDefinition.archived_at.is_(None), visible_condition(principal)
    )
    if tab == "mine":
        stmt = stmt.where(ReportDefinition.owner_id == principal.id)
    elif tab == "shared":
        stmt = stmt.where(ReportDefinition.owner_id != principal.id)
    if q:
        stmt = stmt.where(ReportDefinition.name.ilike(f"%{_escape(q.strip())}%"))
    if status:
        stmt = stmt.where(ReportDefinition.status == status)
    stmt = stmt.order_by(ReportDefinition.updated_at.desc(), ReportDefinition.id)
    page = await paginate(db, stmt, params)
    items = await report_out(db, principal, list(page.items))
    return Page[ReportOut](items=items, total=page.total, page=page.page, page_size=page.page_size)


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_definitions(
    ctx: RequestContext, db: AsyncSession, q: str, limit: int = 10
) -> list[ReportDefinition]:
    """Visibility-filtered name search (consumed by Epic 52's global search)."""
    principal = principal_of(ctx)
    term = q.strip()
    if not term:
        return []
    stmt = (
        select(ReportDefinition)
        .where(
            ReportDefinition.archived_at.is_(None),
            visible_condition(principal),
            ReportDefinition.name.ilike(f"%{_escape(term)}%"),
        )
        .order_by(ReportDefinition.name)
        .limit(max(1, min(limit, 50)))
    )
    return list((await db.execute(stmt)).scalars())


async def quick_search_definitions(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[Any]:
    """Foundation 0.7 owner function: ``QuickHit`` rows of type ``report_definition``."""
    from app.schemas.search import QuickHit

    return [
        QuickHit(
            id=r.id,
            type="report_definition",
            title=r.name,
            subtitle=_source_label(r.source_key),
            human_id=None,
            url=f"/reports/custom/{r.id}",
        )
        for r in await search_definitions(ctx, db, q, limit)
    ]
