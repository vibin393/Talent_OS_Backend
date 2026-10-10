# ruff: noqa: E501
"""Reference checks (Epic 20 A7, P2-7.5, FR-114).

* ``request`` (``reference_check:request``): ``requested`` + candidate notification. Linked to a ``reference``
  BGV check it serves as that check.
* Referees are named by the candidate in the portal (``portal.add_referees``) -> ``send_links``: one Secure Link
  per referee (purpose ``reference_check``, TTL ``reference_link_days``), check ``sent``.
* Public form: GET never mutates; POST validates answers against the snapshot, stores them, consumes the link,
  records ``ip_hash``; decline -> ``declined``. When every referee is terminal the check is ``responded`` (any
  response) or ``expired``.
* Answers (``responses``) need ``scorecard:read`` and never reach a portal schema.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.models.bgv import BgvCheck, Referee, ReferenceCheck, ReferenceResponse
from app.schemas.bgv import (
    RefereeOut,
    ReferenceAnswerOut,
    ReferenceCheckOut,
    ReferenceFormView,
    ReferenceRequestIn,
    ReferenceResponsesOut,
    ReviewIn,
)
from app.services.bgv.common import candidate_names, now, settings_row, tenant_of, user_id
from app.services.bgv.packages import questionnaire_snapshot

PURPOSE = "reference_check"
TERMINAL_REFEREE = ("responded", "declined", "expired")


async def _out(db: AsyncSession, rc: ReferenceCheck) -> ReferenceCheckOut:
    refs = (
        await db.execute(
            select(Referee).where(Referee.reference_check_id == rc.id).order_by(Referee.created_at)
        )
    ).scalars()
    out = ReferenceCheckOut.model_validate(rc)
    return out.model_copy(update={"referees": [RefereeOut.model_validate(r) for r in refs]})


async def get_reference_check(
    db: AsyncSession, reference_check_id: uuid.UUID, *, lock: bool = False
) -> ReferenceCheck:
    stmt = select(ReferenceCheck).where(ReferenceCheck.id == reference_check_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Reference check not found")
    return row


async def request(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: ReferenceRequestIn
) -> ReferenceCheckOut:
    from app.models.candidates import Candidate
    from app.services.bgv.events import ReferenceRequested

    if await db.get(Candidate, candidate_id) is None:
        raise NotFound("Candidate not found")
    case_id = data.case_id
    if data.check_id is not None:
        check = await db.get(BgvCheck, data.check_id)
        if check is None or check.candidate_id != candidate_id or check.type != "reference":
            raise ValidationFailed(
                "Link a reference check of this candidate's BGV case",
                details={"fields": {"check_id": "Invalid"}},
            )
        case_id = check.case_id
    rc = ReferenceCheck(
        id=uuid.uuid4(),
        tenant_id=tenant_of(ctx, db),
        candidate_id=candidate_id,
        case_id=case_id,
        check_id=data.check_id,
        requested_by=user_id(ctx),
        questionnaire_id=data.questionnaire_id,
        questionnaire_snapshot=await questionnaire_snapshot(db, data.questionnaire_id),
        min_referees=data.min_referees,
        status="requested",
    )
    db.add(rc)
    await db.flush()
    if data.check_id is not None:
        check = await db.get(BgvCheck, data.check_id)
        if check is not None:
            check.reference_check_id = rc.id
    emit(db, ReferenceRequested(tenant_id=rc.tenant_id, reference_check_id=rc.id, candidate_id=candidate_id))
    await db.refresh(rc)
    return await _out(db, rc)


async def list_for_candidate(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    candidate_id: uuid.UUID,
) -> list[ReferenceCheckOut]:
    rows = (
        await db.execute(
            select(ReferenceCheck)
            .where(ReferenceCheck.candidate_id == candidate_id)
            .order_by(ReferenceCheck.created_at.desc())
        )
    ).scalars()
    return [await _out(db, r) for r in rows]


async def detail(ctx: RequestContext, db: AsyncSession, reference_check_id: uuid.UUID) -> ReferenceCheckOut:  # noqa: ARG001
    return await _out(db, await get_reference_check(db, reference_check_id))


async def review(
    ctx: RequestContext, db: AsyncSession, reference_check_id: uuid.UUID, data: ReviewIn
) -> ReferenceCheckOut:
    rc = await get_reference_check(db, reference_check_id, lock=True)
    if rc.status not in ("responded", "expired", "declined"):
        raise ConflictError("Only completed reference checks can be reviewed", code="reference_not_complete")
    rc.status = "reviewed"
    rc.reviewed_by = user_id(ctx)
    rc.reviewed_at = now()
    rc.review_note = data.note
    await db.flush()
    await db.refresh(rc)
    return await _out(db, rc)


async def responses(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    reference_check_id: uuid.UUID,
) -> ReferenceResponsesOut:
    rc = await get_reference_check(db, reference_check_id)
    rows = (
        await db.execute(
            select(ReferenceResponse, Referee)
            .join(Referee, Referee.id == ReferenceResponse.referee_id)
            .where(ReferenceResponse.reference_check_id == rc.id)
            .order_by(ReferenceResponse.submitted_at)
        )
    ).all()
    return ReferenceResponsesOut(
        reference_check_id=rc.id,
        questions=list((rc.questionnaire_snapshot or {}).get("questions") or []),
        responses=[
            ReferenceAnswerOut(
                referee_id=ref.id,
                referee_name=ref.name,
                relationship=ref.relationship,
                company=ref.company,
                submitted_at=resp.submitted_at,
                answers=dict(resp.answers or {}),
            )
            for resp, ref in rows
        ],
    )


# ================================================================ sending (called from the portal)
async def send_links(db: AsyncSession, rc: ReferenceCheck, referees: list[Referee]) -> int:
    from app.core.mailer import EmailMessage, send_email

    settings = await settings_row(db, rc.tenant_id)
    names = await candidate_names(db, {rc.candidate_id})
    candidate = names.get(rc.candidate_id) or "a candidate"
    sent = 0
    for ref in referees:
        link, token = await secure_links.issue(
            db,
            tenant_id=rc.tenant_id,
            purpose=PURPOSE,
            subject_type="referee",
            subject_id=ref.id,
            ttl=timedelta(days=settings.reference_link_days),
            max_uses=1,
            metadata={"reference_check_id": str(rc.id)},
        )
        ref.secure_link_id = link.id
        ref.status = "sent"
        ref.sent_at = now()
        ref.expires_at = link.expires_at
        await db.flush()
        await send_email(
            EmailMessage(
                to=str(ref.email),
                subject=f"Reference request for {candidate}",
                text=(
                    f"Hello {ref.name},\n\n{candidate} named you as a referee. Please answer a short questionnaire "
                    f"here (the link expires in {settings.reference_link_days} days):\n{secure_links.link_url(token)}\n"
                ),
                tenant_id=rc.tenant_id,
                kind="reference_check",
            )
        )
        sent += 1
    if sent:
        rc.status = "sent"
        await db.flush()
    return sent


async def finalize(db: AsyncSession, rc: ReferenceCheck) -> None:
    refs = list((await db.execute(select(Referee).where(Referee.reference_check_id == rc.id))).scalars())
    if (
        not refs
        or any(r.status not in TERMINAL_REFEREE for r in refs)
        or rc.status not in ("sent", "requested")
    ):
        return
    rc.status = (
        "responded"
        if any(r.status == "responded" for r in refs)
        else ("declined" if all(r.status == "declined" for r in refs) else "expired")
    )
    await db.flush()


# ================================================================ public form (/public/links/{token}/reference)
async def _resolve(token: str) -> secure_links.ResolvedLink:
    return await secure_links.resolve(token, (PURPOSE,))


async def _referee(
    db: AsyncSession, link: secure_links.ResolvedLink, *, lock: bool = False
) -> tuple[Referee, ReferenceCheck]:
    stmt = select(Referee).where(Referee.id == link.subject_id)
    if lock:
        stmt = stmt.with_for_update()
    ref = (await db.execute(stmt)).scalar_one_or_none()
    if ref is None:
        raise NotFound("Link not found", code="link_invalid")
    rc = await get_reference_check(db, ref.reference_check_id, lock=lock)
    return ref, rc


async def public_view(token: str) -> ReferenceFormView:
    from app.services import identity_bridge

    link = await _resolve(token)
    async with tenant_session(link.tenant_id) as db:
        ref, rc = await _referee(db, link)
        names = await candidate_names(db, {rc.candidate_id})
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
        return ReferenceFormView(
            tenant_name=tenant.name if tenant else None,
            candidate_name=names.get(rc.candidate_id),
            referee_name=ref.name,
            questions=list((rc.questionnaire_snapshot or {}).get("questions") or []),
            status=ref.status,
        )


def validate_answers(questions: list[dict[str, Any]], answers: dict[str, Any]) -> dict[str, Any]:
    from app.core.identity_docs import assert_no_full_aadhaar

    keys = {q["key"] for q in questions}
    unknown = sorted(set(answers) - keys)
    errors: dict[str, str] = dict.fromkeys(unknown, "Unknown question")
    clean: dict[str, Any] = {}
    for q in questions:
        value = answers.get(q["key"])
        if value in (None, ""):
            if q.get("required", True):
                errors[q["key"]] = "Required"
            continue
        kind = q.get("kind", "text")
        if kind == "rating":
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
                errors[q["key"]] = "Choose 1 to 5"
                continue
        elif kind == "yes_no":
            if not isinstance(value, bool):
                errors[q["key"]] = "Answer yes or no"
                continue
        elif not isinstance(value, str) or len(value) > 4000:
            errors[q["key"]] = "Enter up to 4000 characters"
            continue
        clean[q["key"]] = value
    if errors:
        raise ValidationFailed("Please check your answers", details={"fields": errors})
    assert_no_full_aadhaar(*[v for v in clean.values() if isinstance(v, str)])
    return clean


async def public_submit(token: str, answers: dict[str, Any], ip_hash: str | None) -> str:
    link = await _resolve(token)
    async with tenant_session(link.tenant_id) as db:
        ref, rc = await _referee(db, link, lock=True)
        clean = validate_answers(list((rc.questionnaire_snapshot or {}).get("questions") or []), answers)
        await secure_links.consume(db, link.id)
        db.add(
            ReferenceResponse(
                id=uuid.uuid4(),
                tenant_id=rc.tenant_id,
                referee_id=ref.id,
                reference_check_id=rc.id,
                answers=clean,
                submitted_at=now(),
                ip_hash=ip_hash,
            )
        )
        ref.status = "responded"
        ref.responded_at = now()
        await db.flush()
        await finalize(db, rc)
    return "submitted"


async def public_decline(token: str) -> str:
    link = await _resolve(token)
    async with tenant_session(link.tenant_id) as db:
        ref, rc = await _referee(db, link, lock=True)
        await secure_links.consume(db, link.id)
        ref.status = "declined"
        await db.flush()
        await finalize(db, rc)
    return "declined"


# ================================================================ beat: reminders + expiry
async def reminders_and_expiry(db: AsyncSession) -> dict[str, int]:
    from app.core.mailer import EmailMessage, send_email

    current = now()
    out = {"reminded": 0, "expired": 0}
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    settings = await settings_row(db, tenant_id)
    refs = list(
        (
            await db.execute(
                select(Referee).where(Referee.status == "sent").with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    touched: set[uuid.UUID] = set()
    for ref in refs:
        if ref.expires_at is not None and ref.expires_at <= current:
            ref.status = "expired"
            if ref.secure_link_id:
                await secure_links.revoke(db, ref.secure_link_id)
            touched.add(ref.reference_check_id)
            out["expired"] += 1
            continue
        last = ref.last_reminded_at or ref.sent_at
        if last is not None and current - last >= timedelta(days=settings.reference_reminder_days):
            ref.last_reminded_at = current
            await send_email(
                EmailMessage(
                    to=str(ref.email),
                    subject="Reminder: reference request",
                    text=f"Hello {ref.name},\n\nThis is a reminder to complete the reference request we emailed you.",
                    tenant_id=ref.tenant_id,
                    kind="reference_check_reminder",
                )
            )
            out["reminded"] += 1
    await db.flush()
    for rc_id in touched:
        await finalize(db, await get_reference_check(db, rc_id, lock=True))
    return out
