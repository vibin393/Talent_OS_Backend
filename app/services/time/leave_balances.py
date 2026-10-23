"""Leave balance ledger (Epic 26, FR-135): append-only ``leave_balance_entries`` + the ``leave_balances``
cache.

Every ledger write locks the cache row ``FOR UPDATE`` (insert-if-missing via ``ON CONFLICT DO NOTHING`` then
lock) and updates it in the same transaction. Every posting carries a ``source_ref`` that is UNIQUE per
(worker, type, legal entity): system postings are idempotent (``accrual:YYYY-MM``, ``grant:<year>``,
``request:<id>``, ``request-cancel:<id>``, ``carry:<old>-><new>`` / ``lapse:<old>-><new>``,
``golive:<batch>:<row>``). Zero-day postings are never inserted.

Leave is not an AD-23 posting source: leave hours reach payroll / billing only as Timesheet entries.
"""

import dataclasses
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.models.candidates import Candidate
from app.models.placements import Placement
from app.models.time import LeaveBalance, LeaveBalanceEntry, LeaveType
from app.schemas.leave import AdjustBalanceIn, LeaveBalanceOut, LeaveLedgerEntryOut, VerifyBalancesOut
from app.services.time import accruals
from app.services.time.accruals import ZERO_DAYS, q_days
from app.services.time.common import tenant_of
from app.services.time.holidays import leave_settings_row
from app.services.time.leave_events import LeaveAccrued, LeaveBalanceAdjusted
from app.services.time.leave_types import effective_rule, eligible

ELIGIBLE_PLACEMENT_STATUSES = ("active", "offboarding", "ended")


@dataclasses.dataclass(frozen=True, slots=True)
class BalanceKey:
    candidate_id: uuid.UUID
    leave_type_id: uuid.UUID
    legal_entity_id: uuid.UUID
    leave_year: str


async def leave_year_mode(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    return (await leave_settings_row(db, tenant_id)).leave_year


async def lock_balance(db: AsyncSession, tenant_id: uuid.UUID, key: BalanceKey) -> LeaveBalance:
    """Insert-if-missing then ``SELECT ... FOR UPDATE`` (fresh values, never the identity-map copy)."""
    await db.execute(
        pg_insert(LeaveBalance)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=key.candidate_id,
            leave_type_id=key.leave_type_id,
            legal_entity_id=key.legal_entity_id,
            leave_year=key.leave_year,
        )
        .on_conflict_do_nothing(constraint="uq_leave_balances_key")
    )
    return (
        await db.execute(
            select(LeaveBalance)
            .where(
                LeaveBalance.candidate_id == key.candidate_id,
                LeaveBalance.leave_type_id == key.leave_type_id,
                LeaveBalance.legal_entity_id == key.legal_entity_id,
                LeaveBalance.leave_year == key.leave_year,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def post_entry(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    key: BalanceKey,
    *,
    kind: str,
    days: Decimal,
    effective_date: date,
    source_ref: str,
    reason: str | None = None,
    actor: ActorRef | None = None,
    row: LeaveBalance | None = None,
) -> uuid.UUID | None:
    """Append one ledger row (idempotent by ``source_ref``) and update the locked cache row. Returns the new
    entry id, or ``None`` when nothing was inserted (zero days or already posted)."""
    if isinstance(days, float):  # type: ignore[unreachable]
        msg = "float in the leave ledger"  # type: ignore[unreachable]
        raise TypeError(msg)
    days = q_days(days)
    if days == 0:
        return None
    bal = row if row is not None else await lock_balance(db, tenant_id, key)
    entry_id = (
        await db.execute(
            pg_insert(LeaveBalanceEntry)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=key.candidate_id,
                leave_type_id=key.leave_type_id,
                legal_entity_id=key.legal_entity_id,
                leave_year=key.leave_year,
                entry_kind=kind,
                days=days,
                effective_date=effective_date,
                source_ref=source_ref[:120],
                reason=reason,
                actor_kind=(actor.kind if actor is not None else "system"),
                actor_id=(actor.id if actor is not None else None),
            )
            .on_conflict_do_nothing(constraint="uq_leave_balance_entries_source")
            .returning(LeaveBalanceEntry.id)
        )
    ).scalar_one_or_none()
    if entry_id is None:
        return None
    bal.balance = q_days(Decimal(bal.balance) + days)
    await db.flush()
    return entry_id


async def change_pending(
    db: AsyncSession, tenant_id: uuid.UUID, key: BalanceKey, delta: Decimal
) -> LeaveBalance:
    bal = await lock_balance(db, tenant_id, key)
    bal.pending = max(q_days(Decimal(bal.pending) + delta), ZERO_DAYS)
    await db.flush()
    return bal


# ---------------------------------------------------------------- reads
async def balances(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, *, leave_year: str | None = None
) -> list[LeaveBalanceOut]:
    tenant_id = tenant_of(ctx, db)
    year = leave_year or accruals.leave_year_of(_today(), await leave_year_mode(db, tenant_id))
    rows = (
        await db.execute(
            select(LeaveBalance, LeaveType)
            .join(LeaveType, LeaveType.id == LeaveBalance.leave_type_id)
            .where(LeaveBalance.candidate_id == candidate_id, LeaveBalance.leave_year == year)
            .order_by(LeaveType.name)
        )
    ).all()
    return [
        LeaveBalanceOut(
            candidate_id=b.candidate_id,
            leave_type_id=b.leave_type_id,
            leave_type_code=t.code,
            leave_type_name=t.name,
            legal_entity_id=b.legal_entity_id,
            leave_year=b.leave_year,
            balance=b.balance,
            pending=b.pending,
            available=q_days(Decimal(b.balance) - Decimal(b.pending)),
            paid=t.paid,
        )
        for b, t in rows
    ]


async def history(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, *, leave_type_id: uuid.UUID | None = None
) -> list[LeaveLedgerEntryOut]:
    del ctx
    stmt = select(LeaveBalanceEntry).where(LeaveBalanceEntry.candidate_id == candidate_id)
    if leave_type_id is not None:
        stmt = stmt.where(LeaveBalanceEntry.leave_type_id == leave_type_id)
    rows = (
        await db.execute(
            stmt.order_by(LeaveBalanceEntry.effective_date.desc(), LeaveBalanceEntry.created_at.desc())
        )
    ).scalars()
    return [
        LeaveLedgerEntryOut(
            id=e.id,
            leave_type_id=e.leave_type_id,
            legal_entity_id=e.legal_entity_id,
            leave_year=e.leave_year,
            entry_kind=e.entry_kind,
            days=e.days,
            effective_date=e.effective_date,
            source_ref=e.source_ref,
            reason=e.reason,
            actor_kind=e.actor_kind,
            created_at=e.created_at,
        )
        for e in rows
    ]


def _today() -> date:
    from app.services.time.common import today

    return today()


# ---------------------------------------------------------------- adjustments
def _floor_ok(new_balance: Decimal, _pending: Decimal, t: LeaveType) -> bool:
    if not t.allow_negative:
        return new_balance >= 0
    if t.negative_limit is None:
        return True
    return new_balance >= -Decimal(t.negative_limit)


async def adjust(ctx: RequestContext, db: AsyncSession, data: AdjustBalanceIn) -> LeaveBalanceOut:
    tenant_id = tenant_of(ctx, db)
    days = accruals.parse_days(data.days, signed=True)
    if days is None or days == 0:
        raise ValidationFailed("Enter a non-zero number of days (up to 2 decimals)", code="days_invalid")
    from app.services.time.leave_types import get_type

    t = await get_type(db, data.leave_type_id)
    if (
        await db.execute(select(Candidate.id).where(Candidate.id == data.candidate_id))
    ).scalar_one_or_none() is None:
        raise NotFound("Worker not found")
    year = data.leave_year or accruals.leave_year_of(_today(), await leave_year_mode(db, tenant_id))
    key = BalanceKey(data.candidate_id, t.id, data.legal_entity_id, year)
    bal = await lock_balance(db, tenant_id, key)
    new_balance = Decimal(bal.balance) + days
    if days < 0 and not _floor_ok(new_balance, Decimal(bal.pending), t):
        raise ConflictError(
            "This adjustment would take the balance below the allowed minimum",
            code="balance_insufficient",
            details={"balance": str(bal.balance)},
        )
    actor = actor_from_ctx(ctx)
    entry_id = await post_entry(
        db,
        tenant_id,
        key,
        kind="adjustment",
        days=days,
        effective_date=_today(),
        source_ref=f"adjust:{uuid.uuid4()}",
        reason=data.reason,
        actor=actor,
        row=bal,
    )
    audit.record(
        "leave_balance.adjusted",
        "candidate",
        data.candidate_id,
        session=db,
        leave_type_id=str(t.id),
        legal_entity_id=str(data.legal_entity_id),
        leave_year=year,
        days=str(days),
        reason=data.reason,
    )
    if entry_id is not None:
        emit(
            db,
            LeaveBalanceAdjusted(
                tenant_id=tenant_id, candidate_id=data.candidate_id, leave_type_id=t.id, entry_id=entry_id
            ),
        )
    out = [
        b
        for b in await balances(ctx, db, data.candidate_id, leave_year=year)
        if b.leave_type_id == t.id and b.legal_entity_id == data.legal_entity_id
    ]
    return out[0]


# ---------------------------------------------------------------- accruals (beat)
def _overlap_days(p: Placement, first: date, last: date) -> set[date]:
    start = p.actual_start_date or p.planned_start_date
    if start is None:
        return set()  # type: ignore[unreachable]
    end = p.end_date or p.planned_end_date or last
    lo, hi = max(start, first), min(end, last)
    if hi < lo:
        return set()
    return {lo + timedelta(days=i) for i in range((hi - lo).days + 1)}


async def run_monthly_accruals(ctx: RequestContext, db: AsyncSession, month: date) -> int:
    """Post ``accrual:YYYY-MM`` (monthly types) and ``grant:<leave year>`` (annual grants) for every Worker
    with
    an eligible Placement in ``month``. Re-runs (and concurrent runs) post nothing new."""
    tenant_id = tenant_of(ctx, db)
    mode = await leave_year_mode(db, tenant_id)
    first = month.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    days_in_month = (last - first).days + 1
    month_key = f"{first.year:04d}-{first.month:02d}"
    leave_year = accruals.leave_year_of(first, mode)
    types = list(
        (
            await db.execute(
                select(LeaveType).where(
                    LeaveType.archived_at.is_(None), LeaveType.accrual_mode.in_(("monthly", "annual_grant"))
                )
            )
        ).scalars()
    )
    if not types:
        return 0
    placements = list(
        (
            await db.execute(
                select(Placement).where(
                    Placement.status.in_(ELIGIBLE_PLACEMENT_STATUSES),
                    func.coalesce(Placement.actual_start_date, Placement.planned_start_date) <= last,
                )
            )
        ).scalars()
    )
    posted = 0
    for t in types:
        # (candidate, legal entity) -> eligible days + the placement covering most days (its Client's
        # override)
        cover: dict[tuple[uuid.UUID, uuid.UUID], set[date]] = defaultdict(set)
        best: dict[tuple[uuid.UUID, uuid.UUID], tuple[int, Placement]] = {}
        for p in placements:
            if not eligible(t, p.worker_type, p.legal_entity_id):
                continue
            span = _overlap_days(p, first, last)
            if not span:
                continue
            k = (p.candidate_id, p.legal_entity_id)
            cover[k] |= span
            if k not in best or len(span) > best[k][0]:
                best[k] = (len(span), p)
        for (candidate_id, le_id), span in cover.items():
            rule = await effective_rule(db, t, best[candidate_id, le_id][1].client_id)
            key = BalanceKey(candidate_id, t.id, le_id, leave_year)
            try:
                async with db.begin_nested():
                    bal = await lock_balance(db, tenant_id, key)
                    if t.accrual_mode == "monthly":
                        amount = accruals.accrual_for_month(
                            rule.accrual, Decimal(bal.balance), len(span), days_in_month
                        )
                        ref, kind = f"accrual:{month_key}", "accrual"
                    else:
                        ref, kind = f"grant:{leave_year}", "grant"
                        months = accruals.months_remaining(min(span), mode)
                        amount = accruals.annual_grant_amount(rule.accrual, Decimal(bal.balance), months)
                    if amount > 0 and await post_entry(
                        db,
                        tenant_id,
                        key,
                        kind=kind,
                        days=amount,
                        effective_date=last if kind == "accrual" else min(span),
                        source_ref=ref,
                        reason=None,
                        row=bal,
                    ):
                        posted += 1
            except Exception:
                logger.exception("leave accrual failed for candidate {} type {}", candidate_id, t.id)
    if posted:
        emit(db, LeaveAccrued(tenant_id=tenant_id, month=month_key, count=posted))
    return posted


async def run_year_end(ctx: RequestContext, db: AsyncSession, old_year: str) -> int:
    """Carry forward / lapse every balance of ``old_year`` into the next leave year (idempotent)."""
    tenant_id = tenant_of(ctx, db)
    mode = await leave_year_mode(db, tenant_id)
    new_year = accruals.next_leave_year(old_year, mode)
    _, old_end = accruals.leave_year_bounds(old_year, mode)
    new_start = old_end + timedelta(days=1)
    rows = list(
        (
            await db.execute(
                select(LeaveBalance, LeaveType)
                .join(LeaveType, LeaveType.id == LeaveBalance.leave_type_id)
                .where(LeaveBalance.leave_year == old_year, LeaveType.paid.is_(True))
            )
        ).all()
    )
    posted = 0
    for b, t in rows:
        old_key = BalanceKey(b.candidate_id, b.leave_type_id, b.legal_entity_id, old_year)
        new_key = BalanceKey(b.candidate_id, b.leave_type_id, b.legal_entity_id, new_year)
        async with db.begin_nested():
            bal = await lock_balance(db, tenant_id, old_key)
            already = (
                await db.execute(
                    select(LeaveBalanceEntry.id).where(
                        LeaveBalanceEntry.candidate_id == b.candidate_id,
                        LeaveBalanceEntry.leave_type_id == b.leave_type_id,
                        LeaveBalanceEntry.legal_entity_id == b.legal_entity_id,
                        LeaveBalanceEntry.source_ref.in_(
                            (f"carry:{old_year}->{new_year}", f"lapse:{old_year}->{new_year}")
                        ),
                    )
                )
            ).first()
            if already is not None:
                continue
            carry, lapse = accruals.carry_forward(Decimal(bal.balance), t.carry_forward_limit)
            if lapse != 0 and await post_entry(
                db,
                tenant_id,
                old_key,
                kind="lapse",
                days=-lapse,
                effective_date=old_end,
                source_ref=f"lapse:{old_year}->{new_year}",
                row=bal,
            ):
                posted += 1
            if carry != 0 and await post_entry(
                db,
                tenant_id,
                new_key,
                kind="carry_forward",
                days=carry,
                effective_date=new_start,
                source_ref=f"carry:{old_year}->{new_year}",
            ):
                posted += 1
    return posted


async def verify_balances(ctx: RequestContext, db: AsyncSession) -> VerifyBalancesOut:
    """Recompute the ledger sum per cache row; report (and log) every mismatch (AD-37 maintenance)."""
    del ctx
    sums = {
        (r.candidate_id, r.leave_type_id, r.legal_entity_id, r.leave_year): Decimal(r.total)
        for r in (
            await db.execute(
                select(
                    LeaveBalanceEntry.candidate_id,
                    LeaveBalanceEntry.leave_type_id,
                    LeaveBalanceEntry.legal_entity_id,
                    LeaveBalanceEntry.leave_year,
                    func.sum(LeaveBalanceEntry.days).label("total"),
                ).group_by(
                    LeaveBalanceEntry.candidate_id,
                    LeaveBalanceEntry.leave_type_id,
                    LeaveBalanceEntry.legal_entity_id,
                    LeaveBalanceEntry.leave_year,
                )
            )
        ).all()
    }
    mismatches: list[dict[str, str]] = []
    checked = 0
    seen: set[tuple[Any, ...]] = set()
    for b in (await db.execute(select(LeaveBalance))).scalars():
        checked += 1
        k = (b.candidate_id, b.leave_type_id, b.legal_entity_id, b.leave_year)
        seen.add(k)
        expected = q_days(sums.get(k, ZERO_DAYS))
        if q_days(Decimal(b.balance)) != expected:
            mismatches.append(
                {
                    "candidate_id": str(k[0]),
                    "leave_type_id": str(k[1]),
                    "leave_year": k[3],
                    "cached": str(b.balance),
                    "ledger": str(expected),
                }
            )
    for k, total in sums.items():
        if k not in seen and q_days(total) != 0:
            mismatches.append(
                {
                    "candidate_id": str(k[0]),
                    "leave_type_id": str(k[1]),
                    "leave_year": k[3],
                    "cached": "missing",
                    "ledger": str(total),
                }
            )
    if mismatches:
        logger.error("leave balance cache mismatch: {} row(s)", len(mismatches))
    return VerifyBalancesOut(checked=checked, mismatches=mismatches)


# ---------------------------------------------------------------- go-live (Epic 35)
async def import_opening_leave_balance(
    ctx: RequestContext, db: AsyncSession, row: Any, batch_id: Any, *, validate_only: bool = False
) -> dict[str, Any]:
    """Post an ``opening`` entry from a go-live import row (``source_ref='golive:<batch>:<row>'``).

    ``row``: mapping (or object) with ``candidate_id``, ``leave_type_id`` or ``leave_type_code``,
    ``legal_entity_id``, ``days`` (decimal string), optional ``leave_year`` and ``row_number``. Returns
    ``{"ok": bool, "errors": [...], "entry_id": ...}``; ``validate_only`` writes nothing."""
    get = row.get if isinstance(row, dict) else (lambda k, d=None: getattr(row, k, d))
    tenant_id = tenant_of(ctx, db)
    errors: list[str] = []
    try:
        candidate_id = uuid.UUID(str(get("candidate_id")))
        legal_entity_id = uuid.UUID(str(get("legal_entity_id")))
    except (TypeError, ValueError):
        return {"ok": False, "errors": ["candidate_id and legal_entity_id must be ids"], "entry_id": None}
    type_id = get("leave_type_id")
    stmt = (
        select(LeaveType).where(LeaveType.id == uuid.UUID(str(type_id)))
        if type_id
        else select(LeaveType).where(LeaveType.code == str(get("leave_type_code") or ""))
    )
    t = (await db.execute(stmt)).scalar_one_or_none()
    if t is None:
        errors.append("unknown leave type")
    days = accruals.parse_days(get("days"), signed=True)
    if days is None:
        errors.append("days must be a decimal string with at most 2 decimals")
    if (
        await db.execute(select(Candidate.id).where(Candidate.id == candidate_id))
    ).scalar_one_or_none() is None:
        errors.append("unknown worker")
    if errors or validate_only or t is None or days is None:
        return {"ok": not errors, "errors": errors, "entry_id": None}
    year = str(get("leave_year") or accruals.leave_year_of(_today(), await leave_year_mode(db, tenant_id)))
    ref = f"golive:{batch_id}:{get('row_number') or 0}"
    entry_id = await post_entry(
        db,
        tenant_id,
        BalanceKey(candidate_id, t.id, legal_entity_id, year),
        kind="opening",
        days=days,
        effective_date=_today(),
        source_ref=ref,
        reason="Opening balance (go-live import)",
        actor=actor_from_ctx(ctx),
    )
    return {"ok": True, "errors": [], "entry_id": entry_id}
