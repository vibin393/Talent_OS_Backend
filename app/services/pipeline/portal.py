"""Client Portal functions of the pipeline domain (Epic 18, FR-101). Append-only ``client_`` functions (the P1
``portal_port.py`` is untouched).

* Only **sent** submissions of demands the Client User can see (org RLS ``client`` + ``visible_demand_ids``);
  anything else is 404. Projections never carry pay rate, burden, margin, match score, source / vendor,
  candidate email / phone or internal notes (FR-80).
* Feedback reuses ``pipeline.feedback.record_feedback`` (source ``portal``): the client-feedback SLA stops and a
  rejection stays ``rejected_pending`` until the recruiter confirms.
* Slots: the client proposes 1–5 slots or chooses one of the recruiter's offered slots; both create a recruiter
  Task (``tasks_service.create_system_task(source='client_portal')``). Nothing is scheduled until the recruiter
  runs the P1 scheduler.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.events.client_portal import ClientSlotChosen, ClientSlotsProposed
from app.models.candidates import Candidate
from app.models.client_portal import InterviewSlotProposal
from app.models.demands import Demand
from app.models.interviews import Interview
from app.models.pipeline import Application, ClientFeedback, Submission
from app.schemas.pipeline_portal import (
    ClientCompareOut,
    ClientFeedbackSnapshot,
    ClientSlotProposalOut,
    ClientSubmissionOut,
    SlotIn,
    SlotOut,
)
from app.services.client_portal_base import client_of, demand_in_scope, display_name

COMPARE_MIN, COMPARE_MAX = 2, 4
MAX_SLOTS = 5


def _not_found() -> NotFound:
    return NotFound("Submission not found")


async def _visible_submission(ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID) -> Submission:
    _, client_id, _ = client_of(ctx)
    sub = (
        await db.execute(
            select(Submission).where(
                Submission.id == submission_id, Submission.client_id == client_id, Submission.status == "sent"
            )
        )
    ).scalar_one_or_none()
    if sub is None or not await demand_in_scope(ctx, db, sub.demand_id):
        raise _not_found()
    return sub


def _slot_out(p: InterviewSlotProposal) -> ClientSlotProposalOut:
    return ClientSlotProposalOut(
        id=p.id,
        kind=p.kind,
        status=p.status,
        slots=[SlotOut(**s) for s in p.slots],
        chosen_index=p.chosen_index,
        created_at=p.created_at,
    )


async def _project(db: AsyncSession, subs: list[Submission]) -> list[ClientSubmissionOut]:
    if not subs:
        return []
    cand_ids = {s.candidate_id for s in subs}
    candidates = {
        c.id: c for c in (await db.execute(select(Candidate).where(Candidate.id.in_(cand_ids)))).scalars()
    }
    titles = {
        d[0]: d[1]
        for d in (
            await db.execute(
                select(Demand.id, Demand.title).where(Demand.id.in_({s.demand_id for s in subs}))
            )
        ).all()
    }
    latest: dict[uuid.UUID, ClientFeedback] = {}
    for f in (
        await db.execute(
            select(ClientFeedback)
            .where(ClientFeedback.submission_id.in_([s.id for s in subs]))
            .order_by(ClientFeedback.received_at)
        )
    ).scalars():
        latest[f.submission_id] = f
    app_ids = [s.application_id for s in subs]
    interviews: dict[uuid.UUID, str] = {}
    for i in (
        await db.execute(
            select(Interview).where(Interview.application_id.in_(app_ids)).order_by(Interview.starts_at)
        )
    ).scalars():
        interviews[i.application_id] = i.status
    proposals: dict[uuid.UUID, list[InterviewSlotProposal]] = {}
    for p in (
        await db.execute(
            select(InterviewSlotProposal)
            .where(InterviewSlotProposal.application_id.in_(app_ids))
            .order_by(InterviewSlotProposal.created_at)
        )
    ).scalars():
        proposals.setdefault(p.application_id, []).append(p)
    out: list[ClientSubmissionOut] = []
    for s in subs:
        c = candidates.get(s.candidate_id)
        fb = latest.get(s.id)
        out.append(
            ClientSubmissionOut(
                id=s.id,
                human_id=s.human_id,
                demand_id=s.demand_id,
                demand_title=titles.get(s.demand_id),
                candidate_display_name=display_name(c.full_name if c else None),
                headline=c.current_title if c else None,
                experience_months=c.total_experience_months if c else None,
                availability_days=c.notice_period_days if c else None,
                bill_rate=s.bill_rate,
                currency=s.currency,
                rate_unit=s.rate_unit,
                recruiter_summary=s.note,
                has_resume=s.resume_file_id is not None,
                sent_at=s.sent_at,
                feedback_status=s.feedback_status,
                latest_feedback=ClientFeedbackSnapshot(
                    decision=fb.decision, comment=fb.comment, received_at=fb.received_at
                )
                if fb
                else None,
                interview_status=interviews.get(s.application_id),
                slot_proposals=[_slot_out(p) for p in proposals.get(s.application_id, [])],
            )
        )
    return out


async def client_list_submissions(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID | None = None
) -> list[ClientSubmissionOut]:
    from app.core.client_scope import visible_demand_ids

    _, client_id, _ = client_of(ctx)
    stmt = select(Submission).where(Submission.client_id == client_id, Submission.status == "sent")
    if demand_id is not None:
        if not await demand_in_scope(ctx, db, demand_id):
            raise NotFound("Demand not found")
        stmt = stmt.where(Submission.demand_id == demand_id)
    scope = await visible_demand_ids(ctx, db)
    if scope is not None:
        stmt = stmt.where(Submission.demand_id.in_(scope or [uuid.uuid4()]))
    rows = list((await db.execute(stmt.order_by(Submission.sent_at.desc()).limit(500))).scalars())
    return await _project(db, rows)


async def client_get_submission(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID
) -> ClientSubmissionOut:
    sub = await _visible_submission(ctx, db, submission_id)
    return (await _project(db, [sub]))[0]


async def client_compare(
    ctx: RequestContext, db: AsyncSession, submission_ids: list[uuid.UUID]
) -> ClientCompareOut:
    ids = list(dict.fromkeys(submission_ids))
    if not COMPARE_MIN <= len(ids) <= COMPARE_MAX:
        raise ValidationFailed(
            f"Compare {COMPARE_MIN} to {COMPARE_MAX} submissions",
            code="compare_limit",
            details={"max": COMPARE_MAX},
        )
    subs = [await _visible_submission(ctx, db, i) for i in ids]
    if len({s.demand_id for s in subs}) != 1:
        raise ValidationFailed("Compare submissions of one demand", code="compare_limit")
    return ClientCompareOut(demand_id=subs[0].demand_id, items=await _project(db, subs))


async def client_feedback(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, decision: str, comment: str | None
) -> ClientSubmissionOut:
    from app.services.pipeline.feedback import record_feedback

    tenant_id, _, me = client_of(ctx)
    sub = await _visible_submission(ctx, db, submission_id)
    contact_id = getattr(ctx.principal, "contact_id", None)
    async with tenant_session(tenant_id) as idb:
        audit.set_actor(idb, "client_user", me)
        recipients = {str(c) for c in (await idb.get(Submission, sub.id)).recipient_contact_ids or []}  # type: ignore[union-attr]
        if contact_id is None:
            contact_id = await _contact_of(idb, me)
        await record_feedback(
            ctx,
            idb,
            sub.id,
            decision,
            comment,
            source="portal",
            contact_id=contact_id if contact_id is not None and str(contact_id) in recipients else None,
        )
    db.expire_all()
    return await client_get_submission(ctx, db, submission_id)


async def _contact_of(db: AsyncSession, client_user_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.external_auth import ClientUser

    return (
        await db.execute(select(ClientUser.contact_id).where(ClientUser.id == client_user_id))
    ).scalar_one_or_none()


async def client_resume(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID
) -> tuple[bytes, str, str]:
    """The resume file sent with the submission (the client copy), never the candidate's master resume."""
    from app.services import files_service

    tenant_id, _, me = client_of(ctx)
    sub = await _visible_submission(ctx, db, submission_id)
    if sub.resume_file_id is None:
        raise NotFound("Resume not found")
    async with tenant_session(tenant_id) as idb:
        row = await files_service.get_file(idb, sub.resume_file_id)
        if row is None or row.deleted_at is not None:
            raise NotFound("Resume not found")
        data = await files_service.read_file(row)
        audit.record(
            "submission.resume_downloaded",
            "submission",
            sub.id,
            session=idb,
            actor_type="client_user",
            actor_id=me,
        )
        return (
            data,
            f"{sub.human_id}-resume.{'pdf' if row.content_type == 'application/pdf' else 'docx'}",
            row.content_type,
        )


# ================================================================ slots
def _validate_slots(slots: list[SlotIn]) -> list[dict[str, str]]:
    if not 1 <= len(slots) <= MAX_SLOTS:
        raise ValidationFailed(f"Propose 1 to {MAX_SLOTS} slots", code="invalid_slots")
    now = datetime.now(UTC)
    out: list[dict[str, str]] = []
    for s in slots:
        if s.starts_at <= now:
            raise ValidationFailed("Slots must be in the future", code="slot_in_past")
        if s.ends_at - s.starts_at > timedelta(hours=8):
            raise ValidationFailed("A slot cannot be longer than 8 hours", code="invalid_slots")
        out.append({"starts_at": s.starts_at.isoformat(), "ends_at": s.ends_at.isoformat(), "tz": s.tz})
    return out


async def _recruiter_task(
    ctx: RequestContext, db: AsyncSession, app: Application, title: str, body: str
) -> uuid.UUID | None:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    task = await tasks_service.create_system_task(
        ctx,
        db,
        TaskCreate(
            title=title[:200],
            notes=body[:4000],
            owner_id=app.owner_id,
            entity_type="application",
            entity_id=app.id,
            due_at=datetime.now(UTC) + timedelta(days=1),
            priority="high",
        ),
        source="client_portal",
    )
    return task.id


def _fmt_slots(slots: list[dict[str, str]]) -> str:
    return "\n".join(f"{i + 1}. {s['starts_at']} – {s['ends_at']} ({s['tz']})" for i, s in enumerate(slots))


async def client_propose_slots(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, slots: list[SlotIn]
) -> ClientSlotProposalOut:
    tenant_id, client_id, me = client_of(ctx)
    sub = await _visible_submission(ctx, db, submission_id)
    payload = _validate_slots(slots)
    async with tenant_session(tenant_id) as idb:
        audit.set_actor(idb, "client_user", me)
        app = (
            await idb.execute(select(Application).where(Application.id == sub.application_id))
        ).scalar_one()
        task_id = await _recruiter_task(
            ctx,
            idb,
            app,
            f"Confirm interview slot ({sub.human_id})",
            f"The client proposed interview slots for {sub.human_id}:\n{_fmt_slots(payload)}\n"
            "Schedule the interview from the application to confirm.",
        )
        row = InterviewSlotProposal(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            client_id=client_id,
            application_id=sub.application_id,
            submission_id=sub.id,
            demand_id=sub.demand_id,
            kind="client_proposed",
            slots=payload,
            status="open",
            created_by_kind="client_user",
            created_by_id=me,
            task_id=task_id,
        )
        idb.add(row)
        await idb.flush()
        emit(
            idb,
            ClientSlotsProposed(
                tenant_id=tenant_id, proposal_id=row.id, application_id=app.id, task_id=task_id
            ),
        )
        await idb.refresh(row)
        return _slot_out(row)


async def client_choose_slot(
    ctx: RequestContext, db: AsyncSession, proposal_id: uuid.UUID, index: int
) -> ClientSlotProposalOut:
    tenant_id, client_id, me = client_of(ctx)
    p = (
        await db.execute(
            select(InterviewSlotProposal).where(
                InterviewSlotProposal.id == proposal_id,
                InterviewSlotProposal.client_id == client_id,
                InterviewSlotProposal.kind == "recruiter_offered",
            )
        )
    ).scalar_one_or_none()
    if p is None or not await demand_in_scope(ctx, db, p.demand_id):
        raise NotFound("Slot offer not found")
    if p.status != "open":
        raise ConflictError("These slots are no longer open", code="slots_not_open")
    if not 0 <= index < len(p.slots):
        raise ValidationFailed("Unknown slot", code="invalid_slot")
    async with tenant_session(tenant_id) as idb:
        audit.set_actor(idb, "client_user", me)
        row = (
            await idb.execute(
                select(InterviewSlotProposal).where(InterviewSlotProposal.id == p.id).with_for_update()
            )
        ).scalar_one()
        if row.status != "open":
            raise ConflictError("These slots are no longer open", code="slots_not_open")
        app = (
            await idb.execute(select(Application).where(Application.id == row.application_id))
        ).scalar_one()
        chosen = row.slots[index]
        task_id = await _recruiter_task(
            ctx,
            idb,
            app,
            f"Client chose an interview slot ({app.human_id})",
            f"The client chose {chosen['starts_at']} – {chosen['ends_at']} ({chosen['tz']}). "
            "Schedule the interview from the application to confirm.",
        )
        row.status = "chosen"
        row.chosen_index = index
        row.task_id = task_id
        await idb.flush()
        emit(
            idb,
            ClientSlotChosen(
                tenant_id=tenant_id,
                proposal_id=row.id,
                application_id=app.id,
                chosen_index=index,
                task_id=task_id,
            ),
        )
        await idb.refresh(row)
        return _slot_out(row)


# ================================================================ internal (recruiter)
async def offer_slots(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, slots: list[SlotIn]
) -> dict[str, Any]:
    """Recruiter offers slots to the client (``interview:write``); the client chooses in the portal."""
    from app.services.pipeline.common import application_for

    app = await application_for(ctx, db, application_id, "application:read")
    sub = (
        await db.execute(
            select(Submission)
            .where(Submission.application_id == app.id, Submission.status == "sent")
            .order_by(Submission.sent_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sub is None:
        raise ConflictError("Send the submission to the client first", code="submission_not_sent")
    payload = _validate_slots(slots)
    row = InterviewSlotProposal(
        id=uuid.uuid4(),
        tenant_id=app.tenant_id,
        client_id=sub.client_id,
        application_id=app.id,
        submission_id=sub.id,
        demand_id=app.demand_id,
        kind="recruiter_offered",
        slots=payload,
        status="open",
        created_by_kind="user",
        created_by_id=getattr(ctx.principal, "id", None),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return proposal_internal(row)


def proposal_internal(row: InterviewSlotProposal) -> dict[str, Any]:
    return {
        "id": row.id,
        "application_id": row.application_id,
        "kind": row.kind,
        "status": row.status,
        "slots": row.slots,
        "chosen_index": row.chosen_index,
        "task_id": row.task_id,
        "interview_id": row.interview_id,
        "created_at": row.created_at,
    }


async def list_slot_proposals(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> list[dict[str, Any]]:
    from app.services.pipeline.common import application_for

    await application_for(ctx, db, application_id, "application:read")
    rows = (
        await db.execute(
            select(InterviewSlotProposal)
            .where(InterviewSlotProposal.application_id == application_id)
            .order_by(InterviewSlotProposal.created_at.desc())
        )
    ).scalars()
    return [proposal_internal(r) for r in rows]


async def link_scheduled_interview(
    db: AsyncSession, application_id: uuid.UUID, interview_id: uuid.UUID
) -> None:
    """After the recruiter schedules from the Task: open / chosen proposals become ``scheduled``."""
    rows = (
        await db.execute(
            select(InterviewSlotProposal).where(
                InterviewSlotProposal.application_id == application_id,
                InterviewSlotProposal.status.in_(("open", "chosen")),
            )
        )
    ).scalars()
    for r in rows:
        r.status = "scheduled"
        r.interview_id = interview_id
    await db.flush()
