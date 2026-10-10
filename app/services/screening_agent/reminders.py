# ruff: noqa: E501
"""Reminders and expiry (Epic 57 story 57.6; beat every 15 minutes on ``maintenance``).

* Expiry: an open conversation past ``expires_at`` -> ``expired``, run cancelled, recruiter Task,
  ``ScreeningConversationCompleted(outcome="expired")``.
* Reminders: a conversation waiting for the Candidate for ``reminder_after_hours`` with ``reminders_sent <
  max_reminders`` -> when ``screening.reminder`` resolves to ``auto`` for the owner, a short run (``schedule`` /
  ``reminder``, dedup ``screening:rem:<id>:<n>``) that sends one templated reminder; otherwise a Task "Send a screening
  reminder?" (once per reminder number). Quiet hours are enforced by the tool / P2 channel governance.
"""

import dataclasses
from datetime import timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.screening_agent import ScreeningConversation
from app.services import agent_runtime_service as runtime
from app.services import entity_refs
from app.services.screening_agent import conversations
from app.services.screening_agent.common import AGENT_KEY, now, settings_row

REMINDER_STATUSES = ("invited", "consented", "in_progress")


@dataclasses.dataclass
class SweepResult:
    expired: int = 0
    reminder_runs: int = 0
    reminder_tasks: int = 0


async def _mode(db: AsyncSession, conv: ScreeningConversation) -> str:
    principal = await entity_refs.principal_for_user(db, conv.tenant_id, conv.owner_user_id)
    if principal is None:
        return "off"
    ctx = RequestContext(tenant_id=conv.tenant_id, principal=principal)
    try:
        decision = await runtime.effective_mode(ctx, db, agent_key=AGENT_KEY, tool_key="screening.reminder")
    except Exception:
        return "off"
    return decision.mode


def _waiting_since(conv: ScreeningConversation):  # noqa: ANN202
    last_out = conv.last_outbound_at or conv.invited_at
    if last_out is None:
        return None
    if conv.last_inbound_at is not None and conv.last_inbound_at >= last_out:
        return None  # the agent owes the next message, not the Candidate
    return last_out


async def sweep(db: AsyncSession) -> SweepResult:
    out = SweepResult()
    stamp = now()
    settings = await settings_row(db)
    rows = list(
        (
            await db.execute(
                select(ScreeningConversation)
                .where(ScreeningConversation.status.in_(("invited", "consented", "in_progress", "paused")))
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for conv in rows:
        ctx = RequestContext(tenant_id=conv.tenant_id, actor_kind="system")
        if conv.expires_at <= stamp:
            await conversations.expire(ctx, db, conv)
            out.expired += 1
            continue
        if conv.status not in REMINDER_STATUSES or conv.reminders_sent >= settings.max_reminders:
            continue
        since = _waiting_since(conv)
        if since is None or stamp - since < timedelta(hours=settings.reminder_after_hours):
            continue
        n = conv.reminders_sent + 1
        if await _mode(db, conv) == "auto":
            ref = await conversations.start_run(
                ctx, db, conv, trigger_kind="schedule", trigger_key="reminder", mode="reminder",
                dedup_key=f"screening:rem:{conv.id}:{n}", track=False,
            )  # fmt: skip
            if ref.run_id is not None:
                out.reminder_runs += 1
            continue
        marker = f"reminder_task:{n}"
        if any(isinstance(f, dict) and f.get("code") == marker for f in conv.flags or []):
            continue
        await conversations._task(
            db, conv, "Send a screening reminder?",
            "The candidate has not replied to the AI screening. Send a reminder from the conversation, or follow up yourself.",
        )  # fmt: skip
        await conversations.flag(db, conv, marker, "Reminder suggested to the recruiter")
        out.reminder_tasks += 1
    await db.flush()
    if out.expired or out.reminder_runs or out.reminder_tasks:
        logger.info("Screening sweep: {}", out)
    return out


__all__ = ["SweepResult", "sweep"]
