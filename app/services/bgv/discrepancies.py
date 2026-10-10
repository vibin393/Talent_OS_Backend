"""Discrepancies (Epic 20 A6, P2-7.4, FR-113): raise (with a candidate response window), candidate response,
lapse (daily beat), resolve. Every change re-runs the case status automation."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.events import emit
from app.events.bgv import BgvDiscrepancyRaised
from app.models.bgv import CASE_TERMINAL, BgvCase, BgvCheck, BgvDiscrepancy
from app.schemas.bgv import DiscrepancyIn, DiscrepancyOut, ResolveIn
from app.services.bgv import status as st
from app.services.bgv.common import (
    actor,
    add_working_days,
    get_case,
    get_check,
    now,
    record_event,
    settings_row,
    today,
    user_id,
)


async def _email_candidate(db: AsyncSession, case: BgvCase, due: object) -> None:
    from app.core.mailer import EmailMessage, send_email
    from app.core.settings import get_settings
    from app.services import entity_refs

    who = await entity_refs.addressee(db, "candidate", case.candidate_id)
    if who is None or not who.address:
        return
    link = f"{get_settings().frontend_url.rstrip('/')}/portal/work/bgv"
    await send_email(
        EmailMessage(
            to=who.address,
            subject="Background verification: please respond to a finding",
            text=(
                "A detail in your background verification needs your response. You can explain it and attach "
                f"supporting documents until {due}:\n{link}\n"
            ),
            tenant_id=case.tenant_id,
            kind="bgv_discrepancy",
        )
    )


async def raise_discrepancy(
    ctx: RequestContext, db: AsyncSession, check: BgvCheck, case: BgvCase, data: DiscrepancyIn
) -> BgvDiscrepancy:
    from app.core.identity_docs import assert_no_full_aadhaar

    assert_no_full_aadhaar(data.description, data.internal_note)
    settings = await settings_row(db, case.tenant_id)
    due = add_working_days(today(), settings.discrepancy_response_working_days)
    row = BgvDiscrepancy(
        id=uuid.uuid4(),
        tenant_id=case.tenant_id,
        case_id=case.id,
        check_id=check.id,
        candidate_id=case.candidate_id,
        severity=data.severity,
        description=data.description,
        internal_note=data.internal_note,
        status="awaiting_response",
        response_due_on=due,
        raised_by=user_id(ctx),
    )
    db.add(row)
    record_event(db, case, "discrepancy_raised", actor(ctx), check_id=check.id, severity=data.severity)
    await db.flush()
    emit(
        db,
        BgvDiscrepancyRaised(
            tenant_id=case.tenant_id, discrepancy_id=row.id, case_id=case.id, severity=data.severity
        ),
    )
    await _email_candidate(db, case, due)
    return row


async def raise_for_check(
    ctx: RequestContext, db: AsyncSession, check_id: uuid.UUID, data: DiscrepancyIn
) -> DiscrepancyOut:
    check = await get_check(db, check_id, lock=True)
    case = await get_case(db, check.case_id, lock=True)
    if case.status in CASE_TERMINAL or case.status == "initiated":
        raise ConflictError(
            "Discrepancies can be raised only while the case is in verification", code="case_closed"
        )
    if check.status in ("pending", "waived"):
        raise ConflictError("Start the check before raising a discrepancy", code="check_not_started")
    row = await raise_discrepancy(ctx, db, check, case, data)
    if check.status in ("in_progress", "verified", "unable_to_verify"):
        check.status = "discrepancy"
        check.recorded_by = user_id(ctx)
        check.recorded_at = now()
    await db.flush()
    await st.recompute_case_status(db, case)
    return DiscrepancyOut.model_validate(row)


async def resolve(
    ctx: RequestContext, db: AsyncSession, discrepancy_id: uuid.UUID, data: ResolveIn
) -> DiscrepancyOut:
    row = (
        await db.execute(select(BgvDiscrepancy).where(BgvDiscrepancy.id == discrepancy_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Discrepancy not found")
    if row.status == "resolved":
        raise ConflictError("The discrepancy is already resolved", code="already_resolved")
    case = await get_case(db, row.case_id, lock=True)
    if case.status in CASE_TERMINAL:
        raise ConflictError("The case is closed", code="case_closed")
    row.status = "resolved"
    row.resolution_note = data.resolution_note
    row.resolved_by = user_id(ctx)
    row.resolved_at = now()
    record_event(db, case, "discrepancy_resolved", actor(ctx), discrepancy_id=row.id)
    await db.flush()
    await st.recompute_case_status(db, case)
    return DiscrepancyOut.model_validate(row)


async def lapse_due(db: AsyncSession) -> int:
    """Daily beat ``bgv.discrepancy_lapse``: ``awaiting_response`` past its due date -> ``lapsed``."""
    from app.core.actors import ActorRef

    rows = list(
        (
            await db.execute(
                select(BgvDiscrepancy)
                .where(BgvDiscrepancy.status == "awaiting_response", BgvDiscrepancy.response_due_on < today())
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for row in rows:
        row.status = "lapsed"
        case = await get_case(db, row.case_id, lock=True)
        record_event(db, case, "discrepancy_lapsed", ActorRef("system", None), discrepancy_id=row.id)
        await db.flush()
        await st.recompute_case_status(db, case)
    return len(rows)
