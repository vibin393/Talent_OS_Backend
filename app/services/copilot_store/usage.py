# ruff: file-ignore[line-too-long]
"""Copilot admin usage aggregates (Epic 54 A6, ``ai:govern``): per day -- conversations, turns, active users, tokens,
cost, helpful %, failures by reason code; plus feedback comments. Metadata only: no question or answer text is read.

Token / cost / failure counts aggregate ``agent_runs`` metadata of Copilot-origin runs (counters only, the runtime's
own log columns; no payloads)."""

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_runtime import AgentRun
from app.models.copilot import CopilotConversation, CopilotFeedback, CopilotMessage

FAILED = ("failed", "stopped")


def _bounds(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    return datetime.combine(date_from, time.min, UTC), datetime.combine(
        date_to + timedelta(days=1), time.min, UTC
    )


async def daily(db: AsyncSession, date_from: date, date_to: date) -> dict[str, Any]:
    start, end = _bounds(date_from, date_to)
    days: dict[date, dict[str, Any]] = defaultdict(
        lambda: {
            "conversations": 0, "turns": 0, "active_users": 0, "input_tokens": 0, "output_tokens": 0, "cost_micros": 0,
            "helpful": 0, "unhelpful": 0, "failures": {},
        }
    )  # fmt: skip
    day = cast(func.timezone("UTC", CopilotConversation.created_at), Date)
    for d, n in await db.execute(
        select(day, func.count())
        .where(CopilotConversation.created_at >= start, CopilotConversation.created_at < end)
        .group_by(day)
    ):
        days[d]["conversations"] = int(n)
    mday = cast(func.timezone("UTC", CopilotMessage.created_at), Date)
    for d, n, users in await db.execute(
        select(mday, func.count(), func.count(func.distinct(CopilotConversation.user_id)))
        .join(CopilotConversation, CopilotConversation.id == CopilotMessage.conversation_id)
        .where(
            CopilotMessage.role == "user", CopilotMessage.created_at >= start, CopilotMessage.created_at < end
        )
        .group_by(mday)
    ):
        days[d]["turns"], days[d]["active_users"] = int(n), int(users)
    rday = cast(func.timezone("UTC", AgentRun.created_at), Date)
    for d, inp, out, cost in await db.execute(
        select(
            rday,
            func.coalesce(func.sum(AgentRun.input_tokens), 0),
            func.coalesce(func.sum(AgentRun.output_tokens), 0),
            func.coalesce(func.sum(AgentRun.cost_micros), 0),
        )
        .where(AgentRun.origin == "copilot", AgentRun.created_at >= start, AgentRun.created_at < end)
        .group_by(rday)
    ):
        days[d]["input_tokens"], days[d]["output_tokens"], days[d]["cost_micros"] = (
            int(inp),
            int(out),
            int(cost),
        )
    for d, reason, n in await db.execute(
        select(rday, AgentRun.reason_code, func.count())
        .where(
            AgentRun.origin == "copilot",
            AgentRun.status.in_(FAILED),
            AgentRun.created_at >= start,
            AgentRun.created_at < end,
        )
        .group_by(rday, AgentRun.reason_code)
    ):
        days[d]["failures"][str(reason or "unknown")] = int(n)
    fday = cast(func.timezone("UTC", CopilotFeedback.created_at), Date)
    for d, rating, n in await db.execute(
        select(fday, CopilotFeedback.rating, func.count())
        .where(CopilotFeedback.created_at >= start, CopilotFeedback.created_at < end)
        .group_by(fday, CopilotFeedback.rating)
    ):
        days[d][str(rating)] = int(n)
    comments = [
        {"run_id": r.run_id, "rating": r.rating, "comment": r.comment, "created_at": r.created_at}
        for r in (
            await db.execute(
                select(CopilotFeedback)
                .where(
                    CopilotFeedback.comment.is_not(None),
                    CopilotFeedback.created_at >= start,
                    CopilotFeedback.created_at < end,
                )
                .order_by(CopilotFeedback.created_at.desc())
                .limit(200)
            )
        ).scalars()
        if r.comment
    ]
    out_days = []
    for d in sorted(days):
        row = days[d]
        rated = row["helpful"] + row["unhelpful"]
        out_days.append(
            {"day": d, **row, "helpful_pct": round(100.0 * row["helpful"] / rated, 1) if rated else None}
        )
    return {"days": out_days, "comments": comments}


__all__ = ["daily"]
