# ruff: noqa: E501
"""Immigration Cases (Epic 46 A2, FR-233, AD-52): open, list, stage moves, key dates, receipt, * outcome,
withdraw, timeline, dependants.

* Every change writes a ``case_events`` row (the timeline); payloads never carry numbers (only ``last4``).
* Stage moves are validated against the Case's **pinned** ``visa_type_versions`` row (422 ``visa_stage_unknown``).
* Approved / Refused are human only (FR-257): ``core.actors.require_human`` -> 403 ``human_only_action`` for
  ``system`` / ``workflow`` / ``ai`` / ``api_key`` actors.
* Data scope: ``immigration:read`` / ``immigration:manage`` on ``owner_user_id`` / ``business_unit_id``; a Case
  outside the caller's scope is a 404.
"""

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import column, func, or_, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import require_human
from app.core.context import RequestContext
from app.core.encrypted import set_identity_field
from app.core.encrypted import unmask as unmask_field
from app.core.events import emit
from app.core.permissions import forbidden
from app.core.sequences import next_human_id
from app.models.immigration import (
    CaseDependant,
    CaseEvent,
    ImmigrationCase,
    ImmigrationCounsel,
    VisaType,
    VisaTypeVersion,
)
from app.schemas.immigration import (
    CaseCreate,
    CaseEventOut,
    CaseOut,
    CasePatch,
    CaseSummaryOut,
    DependantIn,
    DependantOut,
    DependantPatch,
    KeyDateIn,
    OutcomeIn,
    StageMoveIn,
    StageOut,
    VisaTypeDefinition,
    WithdrawIn,
)
from app.services.immigration import definition as defs
from app.services.immigration import transitions
from app.services.immigration.common import (
    IDENTITY_VIEW,
    IMM_MANAGE,
    IMM_READ,
    OPEN_STATUSES,
    actor_json,
    actor_of,
    conflict,
    has,
    invalid,
    not_found,
    now,
    require,
    scoped,
    tenant_of,
    today,
    user_id,
)
from app.services.immigration.events import ImmigrationCaseStep

_candidates = table("candidates", column("id"), column("full_name"))
_users = table("users", column("id"), column("name"), column("business_unit_id"))
_placements = table("placements", column("id"), column("candidate_id"))

__all__ = ["actor_of"]


# ================================================================ loading
async def pinned(db: AsyncSession, case: ImmigrationCase) -> tuple[VisaTypeVersion, VisaTypeDefinition]:
    v = await db.get(VisaTypeVersion, case.visa_type_version_id)
    assert v is not None
    return v, VisaTypeDefinition.model_validate(v.definition)


async def load_scoped(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, *, write: bool = False
) -> ImmigrationCase:
    require(ctx, IMM_MANAGE if write else IMM_READ)
    stmt = scoped(
        ctx, select(ImmigrationCase).where(ImmigrationCase.id == case_id), IMM_MANAGE if write else IMM_READ
    )
    case = (await db.execute(stmt)).scalar_one_or_none()
    if case is None:
        raise not_found()
    return case


def _event(
    ctx: RequestContext,
    case: ImmigrationCase,
    kind: str,
    *,
    from_stage: str | None = None,
    to_stage: str | None = None,
    **payload: Any,
) -> CaseEvent:
    return CaseEvent(
        id=uuid.uuid4(),
        tenant_id=case.tenant_id,
        case_id=case.id,
        candidate_id=case.candidate_id,
        kind=kind,
        from_stage=from_stage,
        to_stage=to_stage,
        actor=actor_json(ctx),
        payload=payload,
    )


def _step(
    ctx: RequestContext, db: AsyncSession, case: ImmigrationCase, step: str, outcome: str | None = None
) -> None:
    emit(
        db,
        ImmigrationCaseStep(
            tenant_id=case.tenant_id,
            case_id=case.id,
            candidate_id=case.candidate_id,
            step=step,
            outcome=outcome,
            actor_user_id=user_id(ctx),
        ),
    )


# ================================================================ derived values
def overdue(case: ImmigrationCase, d: VisaTypeDefinition, on: date | None = None) -> bool:
    if case.status not in OPEN_STATUSES:
        return False
    on = on or today()
    st = defs.stage(d, case.stage_key)
    if st is not None and st.sla_days and case.stage_entered_at is not None:
        if case.stage_entered_at.date() + timedelta(days=st.sla_days) < on:
            return True
    rfe = (case.key_dates or {}).get("rfe_due")
    return bool(rfe) and date.fromisoformat(str(rfe)) < on


def _next_key_date(case: ImmigrationCase, d: VisaTypeDefinition) -> tuple[date | None, str | None]:
    labels = {k.key: k.label for k in d.key_dates}
    upcoming = sorted(
        (date.fromisoformat(str(v)), k)
        for k, v in (case.key_dates or {}).items()
        if v and date.fromisoformat(str(v)) >= today()
    )
    if not upcoming:
        return None, None
    when, key = upcoming[0]
    return when, labels.get(key, key)


async def _names(
    db: AsyncSession, cand_ids: set[uuid.UUID], user_ids: set[uuid.UUID]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    cands = (
        dict(
            (
                await db.execute(
                    select(_candidates.c.id, _candidates.c.full_name).where(_candidates.c.id.in_(cand_ids))
                )
            )
            .tuples()
            .all()
        )
        if cand_ids
        else {}
    )
    users = (
        dict(
            (await db.execute(select(_users.c.id, _users.c.name).where(_users.c.id.in_(user_ids))))
            .tuples()
            .all()
        )
        if user_ids
        else {}
    )
    return cands, users


class _Ctx:
    """Per-request caches of visa types and pinned versions."""

    def __init__(self) -> None:
        self.types: dict[uuid.UUID, VisaType] = {}
        self.versions: dict[uuid.UUID, tuple[int, VisaTypeDefinition]] = {}

    async def load(self, db: AsyncSession, cases: list[ImmigrationCase]) -> None:
        tids = {c.visa_type_id for c in cases} - set(self.types)
        if tids:
            for vt in (await db.execute(select(VisaType).where(VisaType.id.in_(tids)))).scalars():
                self.types[vt.id] = vt
        vids = {c.visa_type_version_id for c in cases} - set(self.versions)
        if vids:
            for v in (
                await db.execute(select(VisaTypeVersion).where(VisaTypeVersion.id.in_(vids)))
            ).scalars():
                self.versions[v.id] = (v.version_no, VisaTypeDefinition.model_validate(v.definition))


def _summary(
    c: ImmigrationCase, cache: _Ctx, cands: dict[uuid.UUID, str], users: dict[uuid.UUID, str]
) -> dict[str, Any]:
    vt = cache.types.get(c.visa_type_id)
    version_no, d = cache.versions[c.visa_type_version_id]
    st = defs.stage(d, c.stage_key)
    nxt, nxt_label = _next_key_date(c, d)
    return {
        "id": c.id,
        "human_id": c.human_id,
        "candidate_id": c.candidate_id,
        "candidate_name": cands.get(c.candidate_id),
        "visa_type_id": c.visa_type_id,
        "visa_type_name": vt.name if vt else "",
        "visa_type_version_no": version_no,
        "country": c.country,
        "status": c.status,
        "stage_key": c.stage_key,
        "stage_label": st.label if st else None,
        "stage_category": st.category if st else None,
        "stage_entered_at": c.stage_entered_at,
        "owner_user_id": c.owner_user_id,
        "owner_name": users.get(c.owner_user_id),
        "receipt_number_last4": c.receipt_number_last4,
        "next_key_date": nxt,
        "next_key_date_label": nxt_label,
        "overdue": overdue(c, d),
        "created_at": c.created_at,
    }


async def to_out(db: AsyncSession, c: ImmigrationCase) -> CaseOut:
    cache = _Ctx()
    await cache.load(db, [c])
    cands, users = await _names(db, {c.candidate_id}, {c.owner_user_id})
    _, d = cache.versions[c.visa_type_version_id]
    counsel = await db.get(ImmigrationCounsel, c.counsel_id) if c.counsel_id else None
    return CaseOut(
        **_summary(c, cache, cands, users),
        placement_id=c.placement_id,
        project_id=c.project_id,
        demand_id=c.demand_id,
        business_unit_id=c.business_unit_id,
        counsel_id=c.counsel_id,
        counsel_name=counsel.name if counsel else None,
        sponsoring_legal_entity_id=c.sponsoring_legal_entity_id,
        key_dates={k: date.fromisoformat(str(v)) for k, v in (c.key_dates or {}).items() if v},
        outcome_at=c.outcome_at,
        outcome_note=c.outcome_note,
        withdraw_reason=c.withdraw_reason,
        lock_version=c.lock_version,
        stages=[
            StageOut(key=s.key, label=s.label, category=s.category, sla_days=s.sla_days) for s in d.stages
        ],
        key_date_defs=d.key_dates,
        expiry_fields=d.expiry_fields,
        allowed_actions=transitions.actions(c.status),
    )


# ================================================================ create / list / patch
async def _owner_bu(db: AsyncSession, owner: uuid.UUID) -> uuid.UUID | None:
    row = (
        await db.execute(select(_users.c.id, _users.c.business_unit_id).where(_users.c.id == owner))
    ).first()
    if row is None:
        raise invalid("unknown_owner", "Unknown owner", field="owner_user_id")
    return row.business_unit_id


async def _check_links(db: AsyncSession, candidate_id: uuid.UUID, data: CaseCreate | CasePatch) -> None:
    if data.placement_id is not None:
        row = (
            await db.execute(select(_placements.c.candidate_id).where(_placements.c.id == data.placement_id))
        ).first()
        if row is None:
            raise invalid("unknown_placement", "Unknown placement", field="placement_id")
        if row.candidate_id != candidate_id:
            raise invalid(
                "placement_candidate_mismatch",
                "The placement belongs to another person",
                field="placement_id",
            )
    if data.counsel_id is not None and await db.get(ImmigrationCounsel, data.counsel_id) is None:
        raise invalid("unknown_counsel", "Unknown counsel", field="counsel_id")
    for field, tbl in (
        ("project_id", "projects"),
        ("demand_id", "demands"),
        ("sponsoring_legal_entity_id", "legal_entities"),
    ):
        value = getattr(data, field)
        if value is not None:
            t = table(tbl, column("id"))
            if (await db.execute(select(t.c.id).where(t.c.id == value))).first() is None:
                raise invalid(
                    f"unknown_{field.removesuffix('_id')}",
                    f"Unknown {field.removesuffix('_id').replace('_', ' ')}",
                    field=field,
                )


async def create(ctx: RequestContext, db: AsyncSession, data: CaseCreate) -> CaseOut:
    require(ctx, IMM_MANAGE)
    if (
        await db.execute(select(_candidates.c.id).where(_candidates.c.id == data.candidate_id))
    ).first() is None:
        raise invalid("unknown_candidate", "Unknown candidate", field="candidate_id")
    vt = await db.get(VisaType, data.visa_type_id)
    if vt is None or vt.archived_at is not None or not vt.active or vt.current_version_id is None:
        raise invalid("visa_type_unavailable", "Pick an active visa type", field="visa_type_id")
    owner = data.owner_user_id or user_id(ctx)
    if owner is None:
        raise invalid("owner_required", "Pick the case owner", field="owner_user_id")
    await _check_links(db, data.candidate_id, data)
    case = ImmigrationCase(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, "immigration_case", "IMM"),
        candidate_id=data.candidate_id,
        visa_type_id=vt.id,
        visa_type_version_id=vt.current_version_id,
        country=vt.country,
        status="open",
        placement_id=data.placement_id,
        project_id=data.project_id,
        demand_id=data.demand_id,
        owner_user_id=owner,
        business_unit_id=await _owner_bu(db, owner),
        counsel_id=data.counsel_id,
        sponsoring_legal_entity_id=data.sponsoring_legal_entity_id,
        key_dates={},
    )
    db.add(case)
    await db.flush()
    db.add(_event(ctx, case, "opened", visa_type=vt.key))
    await db.flush()
    return await to_out(db, case)


async def list_cases(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    status: str | None = None,
    stage: str | None = None,
    stage_category: str | None = None,
    country: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    visa_type_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    overdue_only: bool = False,
    q: str | None = None,
    permits_expiring_days: int | None = None,
    limit: int = 200,
) -> list[CaseSummaryOut]:
    require(ctx, IMM_READ)
    stmt = scoped(ctx, select(ImmigrationCase))
    if status == "open_any":
        stmt = stmt.where(ImmigrationCase.status.in_(OPEN_STATUSES))
    elif status:
        stmt = stmt.where(ImmigrationCase.status.in_(status.split(",")))
    if stage:
        stmt = stmt.where(ImmigrationCase.stage_key == stage)
    if country:
        stmt = stmt.where(ImmigrationCase.country == country.upper())
    if owner_user_id:
        stmt = stmt.where(ImmigrationCase.owner_user_id == owner_user_id)
    if visa_type_id:
        stmt = stmt.where(ImmigrationCase.visa_type_id == visa_type_id)
    if candidate_id:
        stmt = stmt.where(ImmigrationCase.candidate_id == candidate_id)
    if permits_expiring_days is not None:
        from app.models.immigration import Permit

        on = today()
        stmt = stmt.where(
            ImmigrationCase.id.in_(
                select(Permit.case_id).where(
                    Permit.superseded_by_id.is_(None),
                    Permit.valid_to.is_not(None),
                    Permit.valid_to >= on,
                    Permit.valid_to <= on + timedelta(days=permits_expiring_days),
                )
            )
        )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                ImmigrationCase.human_id.ilike(like),
                ImmigrationCase.candidate_id.in_(
                    select(_candidates.c.id).where(_candidates.c.full_name.ilike(like))
                ),
            )
        )
    rows = list(
        (await db.execute(stmt.order_by(ImmigrationCase.created_at.desc()).limit(min(limit, 500)))).scalars()
    )
    cache = _Ctx()
    await cache.load(db, rows)
    cands, users = await _names(db, {r.candidate_id for r in rows}, {r.owner_user_id for r in rows})
    out = [CaseSummaryOut(**_summary(r, cache, cands, users)) for r in rows]
    if overdue_only:
        out = [o for o in out if o.overdue]
    if stage_category:
        out = [
            o
            for o in out
            if o.stage_category == stage_category or (stage_category == "open" and o.stage_key is None)
        ]
    return out


async def get(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> CaseOut:
    return await to_out(db, await load_scoped(ctx, db, case_id))


async def patch(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, data: CasePatch) -> CaseOut:
    case = await load_scoped(ctx, db, case_id, write=True)
    if data.lock_version is not None and data.lock_version != case.lock_version:
        raise conflict("stale_case", "Someone else changed this case; reload it")
    if case.status not in OPEN_STATUSES:
        raise conflict("case_closed", "A closed case cannot be edited")
    await _check_links(db, case.candidate_id, data)
    fields = data.model_dump(exclude_unset=True, exclude={"lock_version"})
    for k, v in fields.items():
        if k == "owner_user_id":
            if v is None:
                continue
            case.business_unit_id = await _owner_bu(db, v)
        setattr(case, k, v)
    case.lock_version += 1
    await db.flush()
    return await to_out(db, case)


# ================================================================ stage / key dates / receipt
async def move_stage(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, data: StageMoveIn) -> CaseOut:
    case = await load_scoped(ctx, db, case_id, write=True)
    transitions.check(case.status, "in_stage")
    _, d = await pinned(db, case)
    target = defs.stage(d, data.stage_key)
    if target is None:
        raise invalid(
            "visa_stage_unknown",
            f"Stage {data.stage_key} is not part of this case's visa type version",
            stage=data.stage_key,
        )
    if case.stage_key == target.key:
        return await to_out(db, case)
    back = case.stage_key is not None and defs.stage_index(d, target.key) < defs.stage_index(
        d, case.stage_key
    )
    reason = (data.reason or "").strip() or None
    if back and reason is None:
        raise invalid("reason_required", "Give a reason for moving the case back")
    from_stage = case.stage_key
    case.status = "in_stage"
    case.stage_key = target.key
    case.stage_entered_at = now()
    case.lock_version += 1
    db.add(
        _event(
            ctx, case, "stage_changed", from_stage=from_stage, to_stage=target.key, reason=reason, back=back
        )
    )
    await db.flush()
    _step(ctx, db, case, "stage_changed")
    return await to_out(db, case)


async def set_key_date(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, data: KeyDateIn) -> CaseOut:
    case = await load_scoped(ctx, db, case_id, write=True)
    _, d = await pinned(db, case)
    if data.key not in {k.key for k in d.key_dates}:
        raise invalid("key_date_unknown", f"Key date {data.key} is not part of this visa type", key=data.key)
    values = dict(case.key_dates or {})
    if data.date is None:
        values.pop(data.key, None)
    else:
        values[data.key] = data.date.isoformat()
    case.key_dates = values
    case.lock_version += 1
    db.add(_event(ctx, case, "key_date_set", key=data.key, date=data.date.isoformat() if data.date else None))
    await db.flush()
    return await to_out(db, case)


async def set_receipt(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, number: str) -> CaseOut:
    from app.services.immigration.permits import normalise_number

    case = await load_scoped(ctx, db, case_id, write=True)
    set_identity_field(case, "receipt_number", normalise_number(number), case.tenant_id)
    case.lock_version += 1
    db.add(_event(ctx, case, "note", field="receipt_number", last4=case.receipt_number_last4))
    await db.flush()
    return await to_out(db, case)


async def unmask_receipt(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> str:
    case = await load_scoped(ctx, db, case_id)
    if not has(ctx, IDENTITY_VIEW):
        raise forbidden(IDENTITY_VIEW)
    if case.receipt_number is None:
        raise conflict("nothing_to_unmask", "This case has no receipt number")
    value = await unmask_field(ctx, db, case, "receipt_number")
    audit.record("immigration.receipt_unmasked", "immigration_case", case.id, session=db)
    return value


# ================================================================ outcome / withdraw
async def record_outcome(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, data: OutcomeIn
) -> CaseOut:
    require(ctx, IMM_MANAGE)
    require_human(actor_of(ctx))
    case = await load_scoped(ctx, db, case_id, write=True)
    transitions.check(case.status, data.outcome)
    _, d = await pinned(db, case)
    if data.outcome == "refused" and data.permit is not None:
        raise invalid("permit_not_allowed", "A refused case has no permit")
    note = (data.note or "").strip() or None
    case.status = data.outcome
    case.outcome_at = now()
    case.outcome_actor = actor_json(ctx)
    case.outcome_note = note
    case.closed_at = case.outcome_at
    case.lock_version += 1
    db.add(_event(ctx, case, "outcome_recorded", from_stage=case.stage_key, outcome=data.outcome))
    await db.flush()
    if data.outcome == "approved" and data.permit is not None:
        from app.services.immigration import permits

        await permits.record(ctx, db, case, data.permit, d)
    audit.record(
        "immigration.outcome_recorded", "immigration_case", case.id, session=db, outcome=data.outcome
    )
    _step(ctx, db, case, "outcome_recorded", data.outcome)
    return await to_out(db, case)


async def withdraw(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, data: WithdrawIn) -> CaseOut:
    case = await load_scoped(ctx, db, case_id, write=True)
    transitions.check(case.status, "withdrawn")
    case.status = "withdrawn"
    case.withdraw_reason = data.reason.strip()
    case.closed_at = now()
    case.lock_version += 1
    db.add(_event(ctx, case, "withdrawn", from_stage=case.stage_key))
    await db.flush()
    return await to_out(db, case)


async def timeline(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> list[CaseEventOut]:
    await load_scoped(ctx, db, case_id)
    rows = (
        await db.execute(
            select(CaseEvent).where(CaseEvent.case_id == case_id).order_by(CaseEvent.at, CaseEvent.id)
        )
    ).scalars()
    return [
        CaseEventOut(
            id=e.id,
            kind=e.kind,
            from_stage=e.from_stage,
            to_stage=e.to_stage,
            actor=e.actor,
            payload=e.payload,
            at=e.at,
        )
        for e in rows
    ]


# ================================================================ dependants
def _dep_out(d: CaseDependant) -> DependantOut:
    return DependantOut(
        id=d.id,
        name=d.name,
        relationship=d.relationship,
        nationality=d.nationality,
        passport_number_last4=d.passport_number_last4,
        visa_status=d.visa_status,
        notes=d.notes,
    )


async def list_dependants(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> list[DependantOut]:
    await load_scoped(ctx, db, case_id)
    rows = (
        await db.execute(
            select(CaseDependant).where(CaseDependant.case_id == case_id).order_by(CaseDependant.created_at)
        )
    ).scalars()
    return [_dep_out(d) for d in rows]


async def _dependant(db: AsyncSession, case_id: uuid.UUID, dep_id: uuid.UUID) -> CaseDependant:
    dep = await db.get(CaseDependant, dep_id)
    if dep is None or dep.case_id != case_id:
        raise not_found("Dependant")
    return dep


async def add_dependant(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, data: DependantIn
) -> DependantOut:
    case = await load_scoped(ctx, db, case_id, write=True)
    dep = CaseDependant(
        id=uuid.uuid4(),
        tenant_id=case.tenant_id,
        case_id=case.id,
        name=data.name.strip(),
        relationship=data.relationship,
        nationality=data.nationality,
        visa_status=data.visa_status,
        notes=data.notes,
    )
    if data.passport_number:
        from app.services.immigration.permits import normalise_number

        set_identity_field(dep, "passport_number", normalise_number(data.passport_number), case.tenant_id)
    db.add(dep)
    await db.flush()
    return _dep_out(dep)


async def update_dependant(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, dep_id: uuid.UUID, data: DependantPatch
) -> DependantOut:
    await load_scoped(ctx, db, case_id, write=True)
    dep = await _dependant(db, case_id, dep_id)
    fields = data.model_dump(exclude_unset=True)
    if "passport_number" in fields:
        from app.services.immigration.permits import normalise_number

        raw = fields.pop("passport_number")
        set_identity_field(dep, "passport_number", normalise_number(raw) if raw else None, dep.tenant_id)
    for k, v in fields.items():
        if v is not None or k in ("nationality", "notes"):
            setattr(dep, k, v)
    await db.flush()
    return _dep_out(dep)


async def delete_dependant(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, dep_id: uuid.UUID
) -> None:
    from app.models.immigration import CaseDocumentRequest

    await load_scoped(ctx, db, case_id, write=True)
    dep = await _dependant(db, case_id, dep_id)
    used = (
        await db.execute(
            select(func.count()).where(
                CaseDocumentRequest.dependant_id == dep.id, CaseDocumentRequest.document_id.is_not(None)
            )
        )
    ).scalar_one()
    if used:
        raise conflict("dependant_has_documents", "This dependant has uploaded documents; keep the record")
    await db.delete(dep)
    await db.flush()


async def unmask_passport(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, dep_id: uuid.UUID
) -> str:
    await load_scoped(ctx, db, case_id)
    dep = await _dependant(db, case_id, dep_id)
    if not has(ctx, IDENTITY_VIEW):
        raise forbidden(IDENTITY_VIEW)
    if dep.passport_number is None:
        raise conflict("nothing_to_unmask", "No passport number is stored")
    value = await unmask_field(ctx, db, dep, "passport_number")
    audit.record(
        "immigration.passport_unmasked", "immigration_case", case_id, session=db, dependant_id=str(dep.id)
    )
    return value


def emit_step(db: AsyncSession, case: ImmigrationCase, step: str, actor: uuid.UUID | None = None) -> None:
    emit(
        db,
        ImmigrationCaseStep(
            tenant_id=case.tenant_id,
            case_id=case.id,
            candidate_id=case.candidate_id,
            step=step,
            actor_user_id=actor,
        ),
    )
