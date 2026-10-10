"""AI token budget (Epic 12 story 12.4, FR-63).

Usage is the month-to-date sum of ``ai_actions`` tokens (index ``(tenant_id, created_at)``), cached for
``USAGE_CACHE_SECONDS`` per tenant and month (process-local cache; workers each keep their own, so a limit can
be overshot by at most one cache window of traffic).

| Usage   | Behaviour                                                            |
|---------|----------------------------------------------------------------------|
| >= 80 % | alert only                                                           |
| >= 100% | ``bulk=True`` calls get ``budget_exceeded`` (items ``paused_budget``) |
| >= 120% | every call gets ``budget_exceeded``                                  |

Each threshold is alerted once per tenant and month (``ai_budget_alerts`` unique row, then
``AIBudgetThresholdCrossed`` -> tenant admins + operators are emailed after commit).
"""

import dataclasses
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.models.ai import AIAction, AIBudgetAlert

USAGE_CACHE_SECONDS = 60
THRESHOLDS: tuple[int, ...] = (80, 100, 120)
BULK_LIMIT_PCT = 100
ALL_LIMIT_PCT = 120

_cache: dict[tuple[uuid.UUID, str], tuple[float, int]] = {}


@dataclasses.dataclass(frozen=True, slots=True)
class AIBudgetThresholdCrossed:
    tenant_id: uuid.UUID
    period: str
    threshold: int
    used: int
    budget: int


@dataclasses.dataclass(frozen=True, slots=True)
class BudgetState:
    used: int
    budget: int | None
    percent: float
    bulk_blocked: bool
    all_blocked: bool


def period_of(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def month_bounds(period: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(period, "%Y-%m").replace(tzinfo=UTC)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def clear_cache() -> None:
    _cache.clear()


def _tokens() -> Any:
    return func.coalesce(AIAction.input_tokens, 0) + func.coalesce(AIAction.output_tokens, 0)


async def usage(
    db: AsyncSession, tenant_id: uuid.UUID, period: str | None = None, *, fresh: bool = False
) -> int:
    period = period or period_of()
    key = (tenant_id, period)
    hit = _cache.get(key)
    if not fresh and hit is not None and time.monotonic() - hit[0] < USAGE_CACHE_SECONDS:
        return hit[1]
    start, end = month_bounds(period)
    stmt = select(func.coalesce(func.sum(_tokens()), 0)).where(
        AIAction.created_at >= start, AIAction.created_at < end
    )
    used = int((await db.execute(stmt)).scalar_one())
    _cache[key] = (time.monotonic(), used)
    return used


def state_for(used: int, budget: int | None) -> BudgetState:
    if budget is None:
        return BudgetState(used, None, 0.0, bulk_blocked=False, all_blocked=False)
    percent = (used * 100.0 / budget) if budget > 0 else float("inf")
    return BudgetState(
        used, budget, percent, bulk_blocked=percent >= BULK_LIMIT_PCT, all_blocked=percent >= ALL_LIMIT_PCT
    )


async def record_alerts(db: AsyncSession, tenant_id: uuid.UUID, state: BudgetState) -> list[int]:
    """Insert the alert rows for every threshold reached (once per month); returns the new thresholds."""
    if state.budget is None:
        return []
    period = period_of()
    new: list[int] = []
    for threshold in THRESHOLDS:
        if state.percent < threshold:
            break
        inserted = (
            await db.execute(
                pg_insert(AIBudgetAlert)
                .values(id=uuid.uuid4(), tenant_id=tenant_id, period=period, threshold=threshold)
                .on_conflict_do_nothing(index_elements=["tenant_id", "period", "threshold"])
                .returning(AIBudgetAlert.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            new.append(threshold)
            emit(
                db,
                AIBudgetThresholdCrossed(
                    tenant_id=tenant_id,
                    period=period,
                    threshold=threshold,
                    used=state.used,
                    budget=state.budget,
                ),
            )
    return new


async def evaluate(db: AsyncSession, tenant_id: uuid.UUID, budget: int | None) -> BudgetState:
    state = state_for(await usage(db, tenant_id), budget)
    if state.percent >= THRESHOLDS[0]:
        await record_alerts(db, tenant_id, state)
    return state


async def usage_report(
    db: AsyncSession, tenant_id: uuid.UUID, budget: int | None, period: str
) -> dict[str, Any]:
    start, end = month_bounds(period)
    in_month = (AIAction.created_at >= start, AIAction.created_at < end)
    total = await usage(db, tenant_id, period, fresh=True)
    by_feature = {
        feature: int(tokens)
        for feature, tokens in (
            await db.execute(
                select(AIAction.feature, func.coalesce(func.sum(_tokens()), 0))
                .where(*in_month)
                .group_by(AIAction.feature)
                .order_by(AIAction.feature)
            )
        ).all()
    }
    day = cast(func.timezone("UTC", AIAction.created_at), Date)
    by_day: dict[str, int] = {}
    for d, tokens in (
        await db.execute(
            select(day, func.coalesce(func.sum(_tokens()), 0)).where(*in_month).group_by(day).order_by(day)
        )
    ).all():
        by_day[d.isoformat() if isinstance(d, date) else str(d)] = int(tokens)
    state = state_for(total, budget)
    return {
        "month": period,
        "total_tokens": total,
        "budget": budget,
        "percent": None if budget is None else round(state.percent, 1),
        "bulk_paused": state.bulk_blocked,
        "all_blocked": state.all_blocked,
        "by_feature": by_feature,
        "by_day": by_day,
    }


def add_usage(tenant_id: uuid.UUID | None, tokens: int) -> None:
    """Account tokens just recorded by the gateway in the cached month-to-date total (keeps the cache
    exact within this process; a rolled-back action overcounts until the cache expires -- conservative)."""
    if tenant_id is None or not tokens:
        return
    key = (tenant_id, period_of())
    hit = _cache.get(key)
    if hit is not None:
        _cache[key] = (hit[0], hit[1] + tokens)
