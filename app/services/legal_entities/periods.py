"""Accounting periods (AD-22): one row per Legal Entity and month, ``open`` or ``closed``.

The database guard (``tos_period_guard``) refuses inserts / updates / deletes of period-guarded rows whose date
falls in a missing or closed month; ``assert_open`` is the service-level check used before writing. Closing and
reopening are human-only (``ActorRef``), lock the period row ``FOR UPDATE`` and emit
``AccountingPeriodClosed`` / ``AccountingPeriodReopened``. Reopen approval orchestration and UI are Epic 30, which
calls ``reopen`` from its approval callback.

Close checks: domains register ``register_close_check(name, fn)``; ``checklist`` runs them for a month and
``close`` refuses while a blocking check reports open items (409 ``period_close_blocked``).
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, fiscal
from app.core.actors import ActorRef, human_only_action, is_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.registry import Registry
from app.events.legal_entities import AccountingPeriodClosed, AccountingPeriodReopened
from app.models.legal_entities import AccountingPeriod
from app.schemas.legal_entities import AccountingPeriodOut


@dataclasses.dataclass(frozen=True)
class CloseCheckResult:
    name: str
    label: str
    blocking: bool
    count: int
    link_path: str | None


CloseCheck = Callable[[RequestContext, AsyncSession, uuid.UUID, date, date], Awaitable[CloseCheckResult]]

_close_checks: Registry[str, CloseCheck] = Registry("close check")


def register_close_check(name: str, fn: CloseCheck) -> None:
    _close_checks.register(name, fn)


def close_checks() -> dict[str, CloseCheck]:
    return dict(_close_checks.view())


def _tenant(session: AsyncSession) -> uuid.UUID:
    raw = session.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "periods need a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def _period_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


async def ensure_periods(session: AsyncSession, legal_entity_id: uuid.UUID, start: date, end: date) -> int:
    """Create the missing ``open`` months between ``start`` and ``end`` (inclusive); returns the count created.
    Idempotent."""
    if end < start:
        return 0
    tenant_id = _tenant(session)
    created = 0
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        result = await session.execute(
            text(
                "INSERT INTO accounting_periods (id, tenant_id, legal_entity_id, year, month, status) "
                "VALUES (:id, :tid, :le, :y, :m, 'open') ON CONFLICT (legal_entity_id, year, month) DO NOTHING"
            ),
            {"id": uuid.uuid4(), "tid": tenant_id, "le": legal_entity_id, "y": year, "m": month},
        )
        created += int(result.rowcount or 0)  # type: ignore[attr-defined]
        year, month = fiscal.add_months(year, month, 1)
    return created


async def ensure_window(session: AsyncSession, legal_entity_id: uuid.UUID, tz: str, *, months_ahead: int = 2) -> int:
    """Current month (in ``tz``) through ``months_ahead`` months ahead."""
    today = fiscal.today_for(tz)
    year, month = fiscal.add_months(today.year, today.month, months_ahead)
    return await ensure_periods(session, legal_entity_id, today.replace(day=1), date(year, month, 1))


async def assert_open(session: AsyncSession, legal_entity_id: uuid.UUID, doc_date: date) -> None:
    status = (
        await session.execute(
            select(AccountingPeriod.status)
            .where(
                AccountingPeriod.legal_entity_id == legal_entity_id,
                AccountingPeriod.year == doc_date.year,
                AccountingPeriod.month == doc_date.month,
            )
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if status != "open":
        raise ConflictError(
            "The accounting period is closed",
            code="period_closed",
            details={
                "legal_entity_id": str(legal_entity_id),
                "period": _period_key(doc_date.year, doc_date.month),
                "state": "missing" if status is None else "closed",
            },
        )


async def _row(
    session: AsyncSession, legal_entity_id: uuid.UUID, year: int, month: int, *, lock: bool = False
) -> AccountingPeriod:
    if not 1 <= month <= 12:
        raise ValidationFailed("Month must be between 1 and 12", code="invalid_period")
    stmt = select(AccountingPeriod).where(
        AccountingPeriod.legal_entity_id == legal_entity_id,
        AccountingPeriod.year == year,
        AccountingPeriod.month == month,
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Accounting period not found")
    return row


async def get_period(session: AsyncSession, legal_entity_id: uuid.UUID, year: int, month: int) -> AccountingPeriodOut:
    return AccountingPeriodOut.model_validate(await _row(session, legal_entity_id, year, month))


async def list_periods(session: AsyncSession, legal_entity_id: uuid.UUID, year: int | None) -> list[AccountingPeriodOut]:
    stmt = select(AccountingPeriod).where(AccountingPeriod.legal_entity_id == legal_entity_id)
    if year is not None:
        stmt = stmt.where(AccountingPeriod.year == year)
    rows = (await session.execute(stmt.order_by(AccountingPeriod.year, AccountingPeriod.month))).scalars()
    return [AccountingPeriodOut.model_validate(r) for r in rows]


async def checklist(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, year: int, month: int
) -> list[CloseCheckResult]:
    start, end = fiscal.month_bounds(year, month)
    return [await fn(ctx, db, legal_entity_id, start, end) for _, fn in sorted(_close_checks.view().items())]


def _require_human(actor: ActorRef) -> None:
    if not is_human(actor):
        raise human_only_action()


async def close(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, year: int, month: int, *, actor: ActorRef
) -> AccountingPeriodOut:
    _require_human(actor)
    period = await _row(db, legal_entity_id, year, month, lock=True)
    if period.status != "open":
        raise ConflictError("The period is not open", code="period_state", details={"status": period.status})
    blocking = [r for r in await checklist(ctx, db, legal_entity_id, year, month) if r.blocking and r.count > 0]
    if blocking:
        raise ConflictError(
            "Resolve the blocking close checks first",
            code="period_close_blocked",
            details={"checks": [{"name": r.name, "count": r.count} for r in blocking]},
        )
    period.status = "closed"
    period.closed_at = datetime.now(UTC)
    period.closed_by = actor.id
    await db.flush()
    audit.record(
        "period.closed", "accounting_period", period.id, session=db,
        period=_period_key(year, month), actor_kind=actor.kind,
    )  # fmt: skip
    emit(
        db,
        AccountingPeriodClosed(
            tenant_id=period.tenant_id,
            legal_entity_id=legal_entity_id,
            year=year,
            month=month,
            actor_kind=actor.kind,
            actor_id=actor.id,
        ),
    )
    return AccountingPeriodOut.model_validate(period)


async def reopen(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    year: int,
    month: int,
    *,
    actor: ActorRef,
    reason: str,
) -> AccountingPeriodOut:
    _require_human(actor)
    text_reason = (reason or "").strip()
    if not text_reason:
        raise ValidationFailed("A reason is required", code="reason_required")
    period = await _row(db, legal_entity_id, year, month, lock=True)
    if period.status != "closed":
        raise ConflictError("The period is not closed", code="period_state", details={"status": period.status})
    period.status = "open"
    period.closed_at = None
    period.closed_by = None
    period.reopened_count += 1
    await db.flush()
    audit.record(
        "period.reopened", "accounting_period", period.id, session=db,
        period=_period_key(year, month), actor_kind=actor.kind, reason=text_reason[:500],
    )  # fmt: skip
    emit(
        db,
        AccountingPeriodReopened(
            tenant_id=period.tenant_id,
            legal_entity_id=legal_entity_id,
            year=year,
            month=month,
            actor_kind=actor.kind,
            actor_id=actor.id,
            reason=text_reason[:500],
        ),
    )
    return AccountingPeriodOut.model_validate(period)
