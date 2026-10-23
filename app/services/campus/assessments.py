# ruff: file-ignore[line-too-long]
"""Drive assessments (Epic 43 story 43.4, FR-219): bulk invite through ``assessments_service.invite`` and the
assessment event handlers.

* ``invite`` -- every ``eligible`` registration without an invitation (or the given ids) is invited to the drive's
  Assessment with ``context_kind="campus_drive"``, ``context_id=drive_id`` and ``idempotency_key="campus:<reg>"``,
  in batches of 500 (each invite in its own SAVEPOINT; per-item errors). Re-running creates nothing new. The
  ``assessment`` notice is Epic 40's (``send_notice=True``). Deviation (recorded): the loop runs in the request with
  the inviting User as actor -- the assessments contract accepts only user / workflow actors, so a system task
  cannot invite.
* ``AssessmentScored`` -> registration ``assessed`` + ``score``; ``AssessmentInvitationDecided`` with outcome
  ``expired`` / ``cancelled`` -> the registration stays ``eligible`` and is flagged (it can be invited again).
"""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import DomainError
from app.models.campus import Registration
from app.schemas.campus import InviteIn, InviteOut, ItemError
from app.services.campus.common import conflict, drive_for, now

BATCH = 500


async def invite(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, data: InviteIn) -> InviteOut:
    from app.schemas.assessments import AssessmentInviteIn
    from app.services import assessments_service
    from app.services.campus import drives

    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    if drive.status not in ("registration_open", "in_progress"):
        raise conflict("drive_not_running", "Assessments can be sent while the drive is running", status=drive.status)
    if drive.assessment_id is None:
        raise conflict("assessment_missing", "Choose the drive's assessment first")
    if data.deadline_at <= now():
        raise conflict("deadline_in_past", "The deadline must be in the future")
    stmt = select(Registration).where(Registration.drive_id == drive.id, Registration.status == "eligible")
    if data.registration_ids is not None:
        stmt = stmt.where(Registration.id.in_(data.registration_ids))
    rows = list((await db.execute(stmt.order_by(Registration.registered_at, Registration.id))).scalars())
    targets = [r for r in rows if r.assessment_invitation_id is None or r.assessment_flag is not None]
    skipped = len(rows) - len(targets)
    if data.registration_ids is not None:
        skipped += len(set(data.registration_ids) - {r.id for r in rows})
    invited = 0
    batches = 0
    errors: list[ItemError] = []
    for start in range(0, len(targets), BATCH):
        batches += 1
        for reg in targets[start : start + BATCH]:
            key = f"campus:{reg.id}" if reg.assessment_flag is None else f"campus:{reg.id}:{uuid.uuid4().hex[:8]}"
            req = AssessmentInviteIn(
                assessment_id=drive.assessment_id,
                candidate_id=reg.candidate_id,
                context_kind="campus_drive",
                context_id=drive.id,
                application_id=None,
                gating=None,
                deadline_at=data.deadline_at,
                idempotency_key=key,
            )
            try:
                async with db.begin_nested():
                    summary = await assessments_service.invite(c, db, req)
            except DomainError as exc:
                errors.append(ItemError(id=reg.id, code=exc.code, message=exc.message))
                continue
            reg.assessment_invitation_id = summary.id
            reg.assessment_flag = None
            invited += 1
        await db.flush()
    if invited:
        await drives.mark_in_progress(db, drive)
    audit.record("campus.assessments_invited", "campus_drive", drive.id, session=db, invited=invited, skipped=skipped)
    return InviteOut(invited=invited, skipped=skipped, batches=batches, errors=errors)


# ================================================================ handlers (tasks)
async def on_scored(db: AsyncSession, invitation_id: uuid.UUID, score: str | None) -> bool:
    from app.services.campus.registration import set_status

    reg = (
        await db.execute(select(Registration).where(Registration.assessment_invitation_id == invitation_id).with_for_update())
    ).scalar_one_or_none()
    if reg is None:
        return False
    reg.score = Decimal(score) if score is not None else None
    reg.assessment_flag = None
    if reg.status == "eligible":
        set_status(reg, "assessed", "system")
    await db.flush()
    return True


async def on_decided(db: AsyncSession, invitation_id: uuid.UUID, outcome: str) -> bool:
    if outcome not in ("expired", "cancelled"):
        return False
    reg = (
        await db.execute(select(Registration).where(Registration.assessment_invitation_id == invitation_id).with_for_update())
    ).scalar_one_or_none()
    if reg is None or reg.status != "eligible":
        return False
    reg.assessment_flag = outcome
    await db.flush()
    return True
