# ruff: noqa: E501
"""Reconciliation (Epic 76, spec "Queues"): calls stuck non-terminal for longer than ``max_call_minutes + 10`` are
checked with ``OP_CALL_STATUS``; a terminal provider status is applied, otherwise the call ends ``failed``
(``failure_code='stale'``) so its cost reservation is released."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.telephony import OPEN_CALL_STATUSES, VoiceCall
from app.services.telephony import common, ingress
from app.services.telephony import settings as settings_mod


async def stuck_calls(ctx: RequestContext, db: AsyncSession) -> int:
    s = await settings_mod.row(db)
    cutoff = common.now() - timedelta(minutes=s.max_call_minutes + 10)
    calls = list(
        (
            await db.execute(
                select(VoiceCall)
                .where(VoiceCall.status.in_(OPEN_CALL_STATUSES), VoiceCall.requested_at < cutoff)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for call in calls:
        event = await ingress.fetch_status(ctx, db, call)
        if event is not None and event.get("kind") in ("completed", "no_answer", "busy", "failed"):
            await ingress.apply_event(ctx, db, call, event)
        else:
            await ingress.apply_event(ctx, db, call, {"kind": "failed"})
            call.failure_code = "stale"
    await db.flush()
    return len(calls)
