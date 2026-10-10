# ruff: noqa: E501
"""Consent withdrawal (Epic 20 A4): the ``ConsentWithdrawn(purpose='bgv')`` handler.

Sets ``consent_withdrawn_at`` on the candidate's open cases, halts every ``in_progress`` check
(``halted_reason='consent_withdrawn'``), revokes provider links, stops connector jobs and notifies Compliance
(``bgv_consent_withdrawn``). Starting or recording then returns 409 ``bgv_consent_missing``; Compliance cancels
the case (user, reason).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.events import emit
from app.models.bgv import CASE_TERMINAL, BgvCase
from app.services.bgv.cases import stop_work
from app.services.bgv.common import compliance_recipients, now, record_event
from app.services.bgv.events import BgvConsentWithdrawnNotice


async def handle_withdrawn(db: AsyncSession, candidate_id: uuid.UUID) -> int:
    cases = list(
        (
            await db.execute(
                select(BgvCase)
                .where(BgvCase.candidate_id == candidate_id, BgvCase.status.notin_(CASE_TERMINAL))
                .with_for_update()
            )
        ).scalars()
    )
    if not cases:
        return 0
    recipients = tuple(await compliance_recipients(db))
    for case in cases:
        case.consent_withdrawn_at = now()
        await stop_work(db, case.id, halt="consent_withdrawn")
        record_event(db, case, "consent_withdrawn", ActorRef("candidate", candidate_id))
        emit(
            db, BgvConsentWithdrawnNotice(tenant_id=case.tenant_id, case_id=case.id, recipient_ids=recipients)
        )
    await db.flush()
    return len(cases)
