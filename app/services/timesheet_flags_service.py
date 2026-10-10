# ruff: noqa: E501, D401
"""Facade of the ``timesheet_flags`` domain (Epic 63, FR-311 / FR-312). Routers, tools, agents and tasks call these,
never the package internals."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services.timesheet_flags import nudges
from app.services.timesheet_flags.actions import am_of, escalate, send_reminders
from app.services.timesheet_flags.evaluate import (
    counts,
    evaluate,
    list_flags,
    list_for_timesheet,
    load_input,
    review,
    set_explanations,
    supersede,
)
from app.services.timesheet_flags.nudges import plan_for
from app.services.timesheet_flags.rules import LABELS, describe
from app.services.timesheet_flags.ticks import nudge_tick, start_flag_run, start_nudge_run


async def timesheet_owner(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    subject: tuple[str, uuid.UUID] | None,
) -> uuid.UUID | None:
    """Accountable Owner of a Timesheet Agent run: the AM (subject ``user``) or the timesheet's Placement AM."""
    if subject is None:
        return None
    if subject[0] == "user":
        return subject[1]
    if subject[0] == "timesheet":
        return await am_of(db, subject[1])
    return None


async def timesheet_context(db: AsyncSession, timesheet_id: uuid.UUID) -> Any:
    """The rules input of a timesheet (``time.get_context``)."""
    from app.models.time import Timesheet

    ts = await db.get(Timesheet, timesheet_id)
    return (ts, await load_input(db, ts)) if ts is not None else (None, None)


__all__ = [
    "LABELS",
    "am_of",
    "counts",
    "describe",
    "escalate",
    "evaluate",
    "list_flags",
    "list_for_timesheet",
    "nudge_tick",
    "nudges",
    "plan_for",
    "review",
    "send_reminders",
    "set_explanations",
    "start_flag_run",
    "start_nudge_run",
    "supersede",
    "timesheet_context",
    "timesheet_owner",
]
