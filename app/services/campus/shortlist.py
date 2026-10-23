# ruff: file-ignore[line-too-long]
"""Score-based shortlist (Epic 43 story 43.4, FR-219, FR-257).

* ``propose`` -- read only: ``assessed`` registrations by score; ``top_n`` keeps every registration scoring at least the
  N-th score (ties at the cut-off are included and flagged), ``min_score`` keeps scores >= value. Nothing changes.
* ``confirm`` (star, human only: ``core.actors.require_human`` -> 403 ``human_only_action``) -- each registration
  becomes ``shortlisted`` with a P1 Application on the chosen Demand (``pipeline_service.add_candidate`` ->
  ``create_application(source="campus", source_ref=str(registration_id), actor="user")``; an existing Application is
  reused). Per-item results; audited.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import DomainError
from app.models.campus import Registration
from app.schemas.campus import ConfirmIn, ConfirmOut, ConfirmResult, ProposalItem, ProposalOut, ProposeIn
from app.services.campus.common import candidate_facts, conflict, drive_for, institution_names


async def propose(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, data: ProposeIn) -> ProposalOut:
    drive = await drive_for(c, db, drive_id)
    rows = list(
        (
            await db.execute(
                select(Registration)
                .where(Registration.drive_id == drive.id, Registration.status == "assessed", Registration.score.is_not(None))
                .order_by(Registration.score.desc(), Registration.registered_at, Registration.id)
            )
        ).scalars()
    )
    value = Decimal(data.value)
    cutoff: Decimal | None
    if data.mode == "top_n":
        n = int(value)
        if n < 1 or not rows:
            picked, cutoff = [], None
        else:
            cutoff = rows[min(n, len(rows)) - 1].score
            picked = [r for r in rows if r.score is not None and cutoff is not None and r.score >= cutoff]
    else:
        cutoff = value
        picked = [r for r in rows if r.score is not None and r.score >= value]
    tie_flag = data.mode == "top_n" and cutoff is not None and len(picked) > int(value)
    facts = await candidate_facts(db, [r.candidate_id for r in picked])
    names = await institution_names(db, [r.institution_id for r in picked])
    items = [
        ProposalItem(
            registration_id=r.id, candidate_id=r.candidate_id, candidate_name=facts.get(r.candidate_id, (None, None))[0],
            institution_name=names.get(r.institution_id), score=r.score, tie=bool(tie_flag and r.score == cutoff),
        )
        for r in picked
    ]
    return ProposalOut(
        mode=data.mode, value=value, cutoff=cutoff, count=len(items), ties=sum(1 for i in items if i.tie), items=items
    )


async def confirm(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, data: ConfirmIn) -> ConfirmOut:
    from app.services import pipeline_service
    from app.services.campus import drives
    from app.services.campus.registration import set_status

    actor = actor_from_ctx(c)
    require_human(actor)
    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    if drive.status not in ("registration_open", "in_progress"):
        raise conflict("drive_not_running", "The drive is not running", status=drive.status)
    if data.demand_id not in await drives.drive_demand_ids(db, drive.id):
        raise conflict("demand_not_on_drive", "Choose one of the drive's demands")
    regs = {
        r.id: r
        for r in (
            await db.execute(
                select(Registration)
                .where(Registration.id.in_(data.registration_ids), Registration.drive_id == drive.id)
                .with_for_update()
            )
        ).scalars()
    }
    results: list[ConfirmResult] = []
    done = 0
    for rid in dict.fromkeys(data.registration_ids):
        reg = regs.get(rid)
        if reg is None:
            results.append(ConfirmResult(registration_id=rid, error="not_found"))
            continue
        if reg.status != "assessed":
            results.append(ConfirmResult(registration_id=rid, error="not_assessed"))
            continue
        try:
            async with db.begin_nested():
                app_id = await pipeline_service.add_candidate(
                    c, db, data.demand_id, reg.candidate_id, source="campus", source_ref=str(reg.id)
                )
                set_status(reg, "shortlisted", "user")
                reg.shortlisted_by = actor.id
                reg.application_id = app_id
                await db.flush()
        except DomainError as exc:
            results.append(ConfirmResult(registration_id=rid, error=exc.code))
            continue
        done += 1
        results.append(ConfirmResult(registration_id=rid, application_id=app_id))
    audit.record(
        "campus.shortlist_confirmed", "campus_drive", drive.id, session=db, demand_id=str(data.demand_id), shortlisted=done
    )
    return ConfirmOut(shortlisted=done, results=results)
