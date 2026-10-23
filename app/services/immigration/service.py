# ruff: file-ignore[line-too-long]
"""Public API of the ``immigration`` domain (Epic 46; facade ``app.services.immigration_service``).

* ``permits_for(ctx, db, candidate_id, on_date)`` -- a Candidate's permits valid on a date (no numbers).
* ``quick_search(ctx, db, q, limit)`` -- global search hits of type ``immigration_case`` (Epic 52), data-scoped.
* ``import_immigration_case(ctx, db, data, *, validate_only=False)`` -- go-live import (FR-263); optional permit
  columns record the Case Approved with that Permit (the importing user is the human actor).
"""

import uuid
from datetime import date

from sqlalchemy import column, or_, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.immigration import ImmigrationCase, VisaType
from app.schemas.immigration import (
    CaseCreate,
    ImmigrationCaseImportIn,
    OutcomeIn,
    PermitFacts,
    PermitIn,
    StageMoveIn,
)
from app.schemas.search import QuickHit
from app.services.immigration import cases as cases_svc
from app.services.immigration import permits
from app.services.immigration.common import IMM_READ, has, scoped

_candidates = table("candidates", column("id"), column("full_name"))


async def permits_for(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, on_date: date
) -> list[PermitFacts]:
    return await permits.permits_for(ctx, db, candidate_id, on_date)


async def quick_search(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[QuickHit]:
    if ctx.principal is not None and not has(ctx, IMM_READ):
        return []
    term = (q or "").strip()
    if len(term) < 2:
        return []
    like = f"%{term}%"
    stmt = scoped(
        ctx,
        select(ImmigrationCase, VisaType.name, _candidates.c.full_name)
        .join(VisaType, VisaType.id == ImmigrationCase.visa_type_id)
        .join(_candidates, _candidates.c.id == ImmigrationCase.candidate_id)
        .where(or_(ImmigrationCase.human_id.ilike(like), _candidates.c.full_name.ilike(like)))
        .order_by(ImmigrationCase.created_at.desc())
        .limit(max(1, min(limit, 50))),
    )
    out: list[QuickHit] = []
    for case, visa_name, full_name in (await db.execute(stmt)).all():
        out.append(
            QuickHit(
                id=case.id,
                type="immigration_case",
                title=f"{case.human_id} · {full_name}",
                subtitle=f"{visa_name} · {case.country} · {case.status.replace('_', ' ')}",
                human_id=case.human_id,
                url=f"/immigration/cases/{case.id}",
                exact=case.human_id.lower() == term.lower(),
            )
        )
    return out


def _bad(field: str, message: str, code: str = "import_invalid") -> ValidationFailed:
    return ValidationFailed(message, code=code, details={"fields": [{"field": field, "message": message}]})


async def import_immigration_case(
    ctx: RequestContext, db: AsyncSession, data: ImmigrationCaseImportIn, *, validate_only: bool = False
) -> uuid.UUID | None:
    if (
        await db.execute(select(_candidates.c.id).where(_candidates.c.id == data.candidate_id))
    ).first() is None:
        raise _bad("candidate_id", "Unknown candidate")
    vt = (await db.execute(select(VisaType).where(VisaType.key == data.visa_type_key))).scalar_one_or_none()
    if vt is None or vt.archived_at is not None or vt.current_version_id is None:
        raise _bad("visa_type_key", "Unknown or archived visa type")
    from app.services.immigration import visa_types

    _, d = await visa_types.version(db, vt.current_version_id)
    if data.stage_key is not None and data.stage_key not in {s.key for s in d.stages}:
        raise _bad("stage_key", "Stage is not part of the visa type", "visa_stage_unknown")
    if data.permit_number and data.permit_valid_from is None:
        raise _bad("permit_valid_from", "Permit start date is required with a permit number")
    permit: PermitIn | None = None
    if data.permit_number:
        try:
            permits.normalise_number(data.permit_number)
            permit = PermitIn(
                permit_type=data.permit_type,
                number=data.permit_number,
                valid_from=data.permit_valid_from,  # type: ignore[arg-type]
                valid_to=data.permit_valid_to,
            )
            permits.check_expiry(d, permit)
        except ValidationFailed as exc:
            raise _bad(
                "permit_number" if exc.code == "invalid_number_format" else "permit_valid_to",
                exc.message,
                exc.code,
            ) from None
    if validate_only:
        return None
    case = await cases_svc.create(
        ctx,
        db,
        CaseCreate(
            candidate_id=data.candidate_id,
            visa_type_id=vt.id,
            owner_user_id=data.owner_user_id,
            placement_id=data.placement_id,
        ),
    )
    if data.stage_key:
        await cases_svc.move_stage(ctx, db, case.id, StageMoveIn(stage_key=data.stage_key))
    if data.receipt_number:
        await cases_svc.set_receipt(ctx, db, case.id, data.receipt_number)
    if permit is not None:
        await cases_svc.record_outcome(
            ctx, db, case.id, OutcomeIn(outcome="approved", note="Imported", permit=permit)
        )
    return case.id
