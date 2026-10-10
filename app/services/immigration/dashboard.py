# ruff: noqa: E501
"""Immigration dashboard (Epic 46 A5, FR-236). Every Case query is data-scoped (``immigration:read`` on owner / BU).

Tiles: open Cases, overdue Cases, Permits expiring in 30 / 60 / 90 days (non-superseded), and **abroad without a
valid Permit** -- active Placements whose work country (the linked Demand's ``country``, else the country of a
Case linked to the Placement) differs from the home country (the Placement's Legal Entity country, default ``IN``)
and whose Candidate has neither an active Permit for that country nor a valid, non-expired ``visa_work_permit``
Credential. Each tile carries the list-filter params of its drill-down.
"""

import uuid
from collections import Counter
from datetime import timedelta

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal, Scope
from app.models.compliance import Credential, CredentialType
from app.models.immigration import ImmigrationCase, Permit, VisaTypeVersion
from app.schemas.immigration import (
    AtRiskOut,
    CountryCount,
    DashboardOut,
    DashboardTile,
    StageCount,
    VisaTypeDefinition,
)
from app.services.immigration import cases as cases_svc
from app.services.immigration import definition as defs
from app.services.immigration.common import IMM_READ, OPEN_STATUSES, require, scoped, today

_placements = table(
    "placements",
    column("id"),
    column("human_id"),
    column("candidate_id"),
    column("status"),
    column("demand_id"),
    column("legal_entity_id"),
)
_demands = table("demands", column("id"), column("country"))
_entities = table("legal_entities", column("id"), column("country"))
_candidates = table("candidates", column("id"), column("full_name"))
HOME_DEFAULT = "IN"


async def _versions(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, VisaTypeDefinition]:
    if not ids:
        return {}
    rows = (await db.execute(select(VisaTypeVersion).where(VisaTypeVersion.id.in_(ids)))).scalars()
    return {v.id: VisaTypeDefinition.model_validate(v.definition) for v in rows}


async def summary(ctx: RequestContext, db: AsyncSession) -> DashboardOut:
    require(ctx, IMM_READ)
    open_cases = list(
        (
            await db.execute(
                scoped(ctx, select(ImmigrationCase).where(ImmigrationCase.status.in_(OPEN_STATUSES)))
            )
        ).scalars()
    )
    versions = await _versions(db, {c.visa_type_version_id for c in open_cases})
    stage_counts: Counter[tuple[str, str | None, str]] = Counter()
    overdue = 0
    for c in open_cases:
        d = versions.get(c.visa_type_version_id)
        st = defs.stage(d, c.stage_key) if d is not None else None
        if st is None:
            stage_counts["open", None, "Not started"] += 1
        else:
            stage_counts[st.category, st.key, st.label] += 1
        if d is not None and cases_svc.overdue(c, d):
            overdue += 1
    countries = Counter(c.country for c in open_cases)

    on = today()
    in_scope = scoped(ctx, select(ImmigrationCase.id)).scalar_subquery()
    expiring: dict[int, int] = {}
    for days in (30, 60, 90):
        n = (
            await db.execute(
                select(Permit.id).where(
                    Permit.superseded_by_id.is_(None),
                    Permit.valid_to.is_not(None),
                    Permit.valid_to >= on,
                    Permit.valid_to <= on + timedelta(days=days),
                    Permit.case_id.in_(in_scope),
                )
            )
        ).all()
        expiring[days] = len(n)
    risk = await at_risk(ctx, db)
    tiles = [
        DashboardTile(key="open", label="Open cases", count=len(open_cases), filters={"status": "open_any"}),
        DashboardTile(
            key="overdue", label="Overdue", count=overdue, filters={"status": "open_any", "overdue": "true"}
        ),
        *[
            DashboardTile(
                key=f"expiring_{d}",
                label=f"Permits expiring in {d} days",
                count=expiring[d],
                filters={"permits_expiring_days": str(d)},
            )
            for d in (30, 60, 90)
        ],
        DashboardTile(
            key="abroad_without_permit",
            label="Abroad without valid permit",
            count=len(risk),
            filters={"at_risk": "true"},
        ),
    ]
    order = {"open": 0, "preparing": 1, "filed": 2, "pending_decision": 3}
    return DashboardOut(
        tiles=tiles,
        by_stage=[
            StageCount(category=cat, stage_key=key, stage_label=label, count=n)
            for (cat, key, label), n in sorted(
                stage_counts.items(), key=lambda kv: (order.get(kv[0][0], 9), kv[0][2])
            )
        ],
        by_country=[CountryCount(country=k, count=n) for k, n in sorted(countries.items())],
    )


def _tenant_scope(ctx: RequestContext) -> bool:
    p = ctx.principal
    return not isinstance(p, Principal) or p.permissions.get(IMM_READ) is Scope.TENANT


async def at_risk(ctx: RequestContext, db: AsyncSession) -> list[AtRiskOut]:
    require(ctx, IMM_READ)
    on = today()
    rows = (
        await db.execute(
            select(
                _placements.c.id,
                _placements.c.human_id,
                _placements.c.candidate_id,
                _demands.c.country.label("demand_country"),
                _entities.c.country.label("home_country"),
            )
            .select_from(_placements)
            .outerjoin(_demands, _demands.c.id == _placements.c.demand_id)
            .outerjoin(_entities, _entities.c.id == _placements.c.legal_entity_id)
            .where(_placements.c.status == "active")
        )
    ).all()
    if not rows:
        return []
    pids = [r.id for r in rows]
    case_rows = list(
        (
            await db.execute(
                select(ImmigrationCase)
                .where(
                    ImmigrationCase.placement_id.in_(pids),
                    ImmigrationCase.status.in_(("open", "in_stage", "approved")),
                )
                .order_by(ImmigrationCase.created_at.desc())
            )
        ).scalars()
    )
    case_by_placement: dict[uuid.UUID, ImmigrationCase] = {}
    for c in case_rows:
        case_by_placement.setdefault(c.placement_id, c)  # type: ignore[arg-type]
    readable: set[uuid.UUID] | None = None
    if not _tenant_scope(ctx):
        readable = set((await db.execute(scoped(ctx, select(ImmigrationCase.id)))).scalars())
    cand_ids = {r.candidate_id for r in rows}
    permits: dict[uuid.UUID, set[str]] = {}
    for cid, country in (
        await db.execute(
            select(Permit.candidate_id, Permit.country).where(
                Permit.candidate_id.in_(cand_ids),
                Permit.superseded_by_id.is_(None),
                Permit.valid_from <= on,
                (Permit.valid_to.is_(None)) | (Permit.valid_to >= on),
            )
        )
    ).all():
        permits.setdefault(cid, set()).add(country)
    with_credential = set(
        (
            await db.execute(
                select(Credential.candidate_id)
                .join(CredentialType, CredentialType.id == Credential.credential_type_id)
                .where(
                    Credential.candidate_id.in_(cand_ids),
                    CredentialType.category == "visa_work_permit",
                    Credential.verification_status == "valid",
                    Credential.superseded_at.is_(None),
                    Credential.erased_at.is_(None),
                    (Credential.expires_on.is_(None)) | (Credential.expires_on >= on),
                )
            )
        ).scalars()
    )
    names = dict(
        (
            await db.execute(
                select(_candidates.c.id, _candidates.c.full_name).where(_candidates.c.id.in_(cand_ids))
            )
        )
        .tuples()
        .all()
    )
    out: list[AtRiskOut] = []
    for r in rows:
        case = case_by_placement.get(r.id)
        work = (r.demand_country or (case.country if case is not None else None) or "").upper()
        home = (r.home_country or HOME_DEFAULT).upper()
        if not work or work == home:
            continue
        if work in permits.get(r.candidate_id, set()) or r.candidate_id in with_credential:
            continue
        if readable is not None and (case is None or case.id not in readable):
            continue
        out.append(
            AtRiskOut(
                placement_id=r.id,
                placement_human_id=r.human_id,
                candidate_id=r.candidate_id,
                candidate_name=names.get(r.candidate_id),
                work_country=work,
                case_id=case.id if case is not None else None,
                case_human_id=case.human_id if case is not None else None,
            )
        )
    return out
