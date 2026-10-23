"""Payroll Export versions (Epic 28 A3-A5, A7; FR-141 / FR-142; AD-21 / AD-23).

* ``collect`` pulls the approved, unposted payable items of ``payee_kind='worker'`` placements of the period's
  Legal Entity (``postings.load_unposted``), maps each to a pay period (``pay_periods.map_period``), filters
  worker types (Vendor-supplied Workers are never here: they are ``payee_kind='vendor'``), prices them
  (``earnings``) and lists the exceptions.
* ``generate_preview``: any number; **no postings, no locks**; a newer preview supersedes older ones. On a
  period that already has a final, the preview is a supplementary preview (unposted items only, no monthly
  base).
* ``finalize`` (human only, Idempotency-Key at the router): one transaction -- lock the period, re-collect,
  compare ``items_hash`` (409 ``preview_stale``), write the ``final`` / ``supplementary`` version, post every
  item to ``payable_postings`` exactly once (``postings.record_posted``; a second posting of a source is 409
  ``source_already_posted``), lock the period, ``content_hash``. Connector pushes are queued as
  ``connector_jobs`` (dispatched after commit; no external I/O here).
* ``mark_processed``: final / supplementary only, idempotent; expense claims -> Reimbursed in the same
  transaction (``expenses_service.mark_reimbursed``).
"""

import dataclasses
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.payroll import PayPeriodLocked, PayrollExportFinalized
from app.models.candidates import Candidate
from app.models.payroll import PayPeriod, PayrollExportItem, PayrollExportRow, PayrollExportVersion
from app.models.placements import Placement, RateSegment, WorkerProfile
from app.schemas.payroll import ExceptionItem, ExceptionsOut, ItemOut, PreviewIn, RowOut, VersionOut
from app.services import postings
from app.services.payroll import earnings
from app.services.payroll.earnings import PricedItem
from app.services.payroll.events import PayrollExportProcessed, PayrollPreviewGenerated
from app.services.payroll.pay_periods import (
    get_settings_row,
    legal_entity,
    load_period,
    map_period,
    now,
    period_views,
    tenant_of,
    user_of,
)

TARGET_KIND = "payroll_row"
BLOCKING_KINDS = ("missing_pay_rate", "missing_bank_details", "currency_mismatch")
EXCEPTION_KINDS = ("unapproved_timesheets", *BLOCKING_KINDS)
ACTIVE_STATUSES = ("active", "offboarding", "ended")


# ================================================================ collect
@dataclasses.dataclass
class Collected:
    items: list[PricedItem]
    exceptions: dict[str, list[dict[str, Any]]]
    identities: dict[uuid.UUID, Any]
    worker_types: dict[uuid.UUID, str]

    @property
    def blocking(self) -> bool:
        return any(self.exceptions.get(k) for k in BLOCKING_KINDS)

    def counts(self) -> dict[str, int]:
        return {k: len(self.exceptions.get(k, [])) for k in EXCEPTION_KINDS}


def _exc(kind: str, **kw: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        **{k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in kw.items() if v is not None},
    }


class _Rates:
    """Caches of the upstream contracts (placements / time); missing contracts degrade to exceptions."""

    def __init__(self, ctx: RequestContext, db: AsyncSession, start: Any, end: Any) -> None:
        self.ctx, self.db, self.start, self.end = ctx, db, start, end
        self.ctx_cache: dict[tuple[uuid.UUID, Any], Any] = {}
        self.inputs_cache: dict[uuid.UUID, Any] = {}

    async def pay_context(self, pid: uuid.UUID, on: Any) -> Any:
        key = (pid, on)
        if key not in self.ctx_cache:
            from app.services import placements_service

            try:
                self.ctx_cache[key] = await placements_service.pay_context(self.ctx, self.db, pid, on)
            except (NotFound, NotImplementedError):
                self.ctx_cache[key] = None
        return self.ctx_cache[key]

    async def pay_inputs(self, pid: uuid.UUID, start: Any = None, end: Any = None) -> Any:
        if start is not None:
            return await self._inputs(pid, start, end)
        if pid not in self.inputs_cache:
            self.inputs_cache[pid] = await self._inputs(pid, self.start, self.end)
        return self.inputs_cache[pid]

    async def _inputs(self, pid: uuid.UUID, start: Any, end: Any) -> Any:
        from app.services import time_service

        try:
            return await time_service.pay_inputs(self.ctx, self.db, pid, start, end)
        except (NotFound, NotImplementedError):
            return None


def _multiplier(inputs: Any, category: str | None, attrs: Any = None) -> Decimal:
    if attrs is not None and attrs.get("multiplier") is not None:
        return Decimal(str(attrs["multiplier"]))
    mults = getattr(inputs, "pay_multipliers", None) or {}
    if category and category in mults:
        return Decimal(str(mults[category]))
    return Decimal(1)


async def _identity(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, *, unmask: bool = False
) -> Any:
    from app.services import placements_service

    try:
        return await placements_service.worker_payment_identity(ctx, db, candidate_id, unmask=unmask)
    except NotImplementedError:
        return None


async def _unapproved(ctx: RequestContext, db: AsyncSession, period: PayPeriod) -> list[dict[str, Any]]:
    from app.services import time_service

    try:
        rows = await time_service.unapproved_timesheets(
            ctx, db, period.legal_entity_id, period.period_start, period.period_end
        )
    except NotImplementedError:
        return []
    return [
        _exc(
            "unapproved_timesheets",
            timesheet_id=t.timesheet_id,
            placement_id=t.placement_id,
            candidate_id=t.candidate_id,
            label=t.human_id,
            detail={"status": t.status, "period_start": t.period_start.isoformat()},
        )
        for t in rows
    ]


async def collect(
    ctx: RequestContext, db: AsyncSession, period: PayPeriod, *, supplementary: bool, exclude_exceptions: bool
) -> Collected:
    """Pull, map, filter and price the period's items. Exclusion (``exclude_exceptions``) is applied by the
    callers; ``collect`` always lists every exception and drops only items it cannot price or pay."""
    del exclude_exceptions
    settings = await get_settings_row(db)
    le = await legal_entity(db, period.legal_entity_id)
    views = await period_views(db, period.legal_entity_id)
    include = set(settings.include_worker_types or ())
    raw = await postings.load_unposted(
        ctx,
        db,
        postings.SourceQuery(
            ledger="payable",
            period_end=period.period_end,
            legal_entity_id=period.legal_entity_id,
            payee_kind="worker",
        ),
    )
    mapped = []
    for it in raw:
        target = map_period(
            it.item_date, it.attributes.get("approved_at"), settings.late_item_mapping, views, le.timezone
        )
        if target is not None and target.id == period.id:
            mapped.append(it)
    pids = {it.placement_id for it in mapped if it.placement_id is not None}
    placements: dict[uuid.UUID, Placement] = {}
    if pids:
        placements = {
            p.id: p for p in (await db.execute(select(Placement).where(Placement.id.in_(pids)))).scalars()
        }
    rates = _Rates(ctx, db, period.period_start, period.period_end)
    exceptions: dict[str, list[dict[str, Any]]] = {k: [] for k in EXCEPTION_KINDS}
    exceptions["unapproved_timesheets"] = await _unapproved(ctx, db, period)
    priced: list[PricedItem] = []
    worker_types: dict[uuid.UUID, str] = {}
    for it in sorted(mapped, key=lambda i: (i.item_date, i.source_kind, str(i.source_id))):
        p = placements.get(it.placement_id) if it.placement_id else None
        if p is None or p.payee_kind != "worker" or (p.worker_type or "employee") not in include:
            continue
        cid = it.attributes.get("candidate_id") or p.candidate_id
        cid = uuid.UUID(str(cid))
        worker_types[cid] = p.worker_type or "employee"
        category = it.attributes.get("category")
        ccy = (it.currency or p.currency).upper()
        if it.source_kind in earnings.ONE_OFF_KINDS:  # Epic 42: fixed amount, adjustments column
            if it.amount is None:
                continue
            priced.append(
                PricedItem(
                    it.source_kind,
                    it.source_id,
                    p.id,
                    cid,
                    ccy,
                    category,
                    it.item_date,
                    Decimal(it.quantity),
                    None,
                    None,
                    Decimal(it.amount),
                )
            )
            continue
        if it.source_kind in earnings.EXPENSE_KINDS:
            if not it.attributes.get("reimbursable", True) or it.amount is None:
                continue
            priced.append(
                PricedItem(
                    it.source_kind,
                    it.source_id,
                    p.id,
                    cid,
                    ccy,
                    category,
                    it.item_date,
                    Decimal(it.quantity),
                    None,
                    None,
                    Decimal(it.amount),
                )
            )
            continue
        inputs = await rates.pay_inputs(p.id)
        if it.source_kind == earnings.RATE_ADJUSTMENT_KIND:
            delta = Decimal(str(it.attributes.get("rate_delta") or 0))
            mult = _multiplier(inputs, category, it.attributes)
            amount = earnings.price_rate_adjustment(Decimal(it.quantity), delta, mult, ccy)
            priced.append(
                PricedItem(
                    it.source_kind,
                    it.source_id,
                    p.id,
                    cid,
                    ccy,
                    category,
                    it.item_date,
                    Decimal(it.quantity),
                    delta,
                    mult,
                    amount,
                )
            )
            continue
        pc = await rates.pay_context(p.id, it.item_date)
        if pc is None or pc.pay_rate is None or pc.rate_unit not in ("hour", "day", "month"):
            exceptions["missing_pay_rate"].append(
                _exc(
                    "missing_pay_rate",
                    placement_id=p.id,
                    candidate_id=cid,
                    source_kind=it.source_kind,
                    source_id=it.source_id,
                    detail={"item_date": it.item_date.isoformat()},
                )
            )
            continue
        if (pc.currency or ccy).upper() != ccy:
            exceptions["currency_mismatch"].append(
                _exc(
                    "currency_mismatch",
                    placement_id=p.id,
                    candidate_id=cid,
                    source_kind=it.source_kind,
                    source_id=it.source_id,
                    detail={"item_currency": ccy, "rate_currency": pc.currency},
                )
            )
            continue
        mult = _multiplier(inputs, category)
        hpd = Decimal(str(inputs.hours_per_day)) if inputs is not None and inputs.hours_per_day else None
        heq = (
            Decimal(str(inputs.hourly_equivalent))
            if inputs is not None and inputs.hourly_equivalent
            else None
        )
        try:
            amount, rate_used = earnings.price_time(
                quantity=Decimal(it.quantity),
                rate=Decimal(pc.pay_rate),
                rate_unit=pc.rate_unit,
                multiplier=mult,
                category=category,
                currency=ccy,
                hours_per_day=hpd,
                hourly_equivalent=heq,
            )
        except ValueError as exc:
            exceptions["missing_pay_rate"].append(
                _exc(
                    "missing_pay_rate",
                    placement_id=p.id,
                    candidate_id=cid,
                    source_kind=it.source_kind,
                    source_id=it.source_id,
                    detail={"reason": str(exc)},
                )
            )
            continue
        priced.append(
            PricedItem(
                it.source_kind,
                it.source_id,
                p.id,
                cid,
                ccy,
                category,
                it.item_date,
                Decimal(it.quantity),
                rate_used if rate_used is not None else Decimal(pc.pay_rate),
                mult,
                amount,
                pc.rate_segment_id,
                hpd,
            )
        )
    if not supplementary:
        priced.extend(await _monthly_base(db, period, include, rates, exceptions, worker_types))
    # identity / bank details per Worker
    identities: dict[uuid.UUID, Any] = {}
    for cid in sorted({i.candidate_id for i in priced}, key=str):
        ident = await _identity(ctx, db, cid)
        identities[cid] = ident
        if ident is None or not ident.bank_active:
            exceptions["missing_bank_details"].append(_exc("missing_bank_details", candidate_id=cid))
    blocked_workers = {uuid.UUID(e["candidate_id"]) for e in exceptions["missing_bank_details"]}
    items = [i for i in priced if i.candidate_id not in blocked_workers]
    return Collected(items, exceptions, identities, worker_types)


async def _monthly_base(
    db: AsyncSession,
    period: PayPeriod,
    include: set[str],
    rates: _Rates,
    exceptions: dict[str, list[dict[str, Any]]],
    worker_types: dict[uuid.UUID, str],
) -> list[PricedItem]:
    """One monthly base item per monthly Rate Segment span overlapping the period (proration from time)."""
    rows = await db.execute(
        select(Placement, RateSegment)
        .join(RateSegment, RateSegment.placement_id == Placement.id)
        .where(
            Placement.legal_entity_id == period.legal_entity_id,
            Placement.payee_kind == "worker",
            Placement.status.in_(ACTIVE_STATUSES),
            Placement.actual_start_date.is_not(None),
            Placement.actual_start_date <= period.period_end,
            (Placement.end_date.is_(None)) | (Placement.end_date >= period.period_start),
            RateSegment.rate_unit == "month",
            RateSegment.effective_from <= period.period_end,
            (RateSegment.effective_to.is_(None)) | (RateSegment.effective_to >= period.period_start),
        )
        .order_by(Placement.id, RateSegment.effective_from)
    )
    out: list[PricedItem] = []
    for p, seg in rows.all():
        if (p.worker_type or "employee") not in include or getattr(seg, "status", "active") not in (
            "active",
            None,
        ):
            continue
        if seg.pay_rate is None:
            continue
        start = max(period.period_start, seg.effective_from)
        end = min(period.period_end, seg.effective_to) if seg.effective_to else period.period_end
        inputs = await rates.pay_inputs(p.id, start, end)
        if inputs is None:
            exceptions["missing_pay_rate"].append(
                _exc(
                    "missing_pay_rate",
                    placement_id=p.id,
                    candidate_id=p.candidate_id,
                    detail={"reason": "no_pay_inputs"},
                )
            )
            continue
        ccy = (seg.currency or p.currency).upper()
        amount = earnings.monthly_base(Decimal(seg.pay_rate), Decimal(str(inputs.proration_factor)), ccy)
        worker_types[p.candidate_id] = p.worker_type or "employee"
        out.append(
            PricedItem(
                earnings.MONTHLY_BASE_KIND,
                None,
                p.id,
                p.candidate_id,
                ccy,
                "base",
                start,
                Decimal(1),
                Decimal(seg.pay_rate),
                None,
                amount,
                seg.id,
            )
        )
    return out


# ================================================================ read models
def version_out(v: PayrollExportVersion) -> VersionOut:
    return VersionOut(
        id=v.id,
        human_id=v.human_id,
        pay_period_id=v.pay_period_id,
        legal_entity_id=v.legal_entity_id,
        kind=v.kind,
        supplementary=v.supplementary,
        seq=v.seq,
        status=v.status,
        based_on_preview_id=v.based_on_preview_id,
        exclude_exceptions=v.exclude_exceptions,
        generated_at=v.generated_at,
        finalized_at=v.finalized_at,
        items_hash=v.items_hash,
        content_hash=v.content_hash,
        exceptions=dict(v.exceptions or {}),
        totals=dict(v.totals or {}),
        row_count=v.row_count,
        item_count=v.item_count,
        provider_status=v.provider_status,
        provider_reference=v.provider_reference,
        processed_at=v.processed_at,
    )


async def load_version(
    db: AsyncSession, version_id: uuid.UUID, *, lock: bool = False
) -> PayrollExportVersion:
    stmt = select(PayrollExportVersion).where(PayrollExportVersion.id == version_id)
    if lock:
        stmt = stmt.with_for_update()
    v = (await db.execute(stmt)).scalar_one_or_none()
    if v is None:
        raise NotFound("Payroll export version not found")
    return v


async def list_versions(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    period_id: uuid.UUID | None = None,
) -> list[VersionOut]:
    stmt = select(PayrollExportVersion).order_by(
        PayrollExportVersion.created_at.desc(), PayrollExportVersion.seq.desc()
    )
    if period_id is not None:
        stmt = stmt.where(PayrollExportVersion.pay_period_id == period_id)
    return [version_out(v) for v in (await db.execute(stmt)).scalars()]


async def get_version(ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID) -> VersionOut:  # ruff: ignore[unused-function-argument]
    return version_out(await load_version(db, version_id))


async def version_rows(db: AsyncSession, version_id: uuid.UUID) -> list[PayrollExportRow]:
    return list(
        (
            await db.execute(
                select(PayrollExportRow)
                .where(PayrollExportRow.version_id == version_id)
                .order_by(PayrollExportRow.worker_human_id, PayrollExportRow.currency, PayrollExportRow.id)
            )
        ).scalars()
    )


async def worker_names(db: AsyncSession, candidate_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not candidate_ids:
        return {}
    rows = await db.execute(
        select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(list(candidate_ids)))
    )
    return dict(rows.tuples().all())


def row_out(r: PayrollExportRow, name: str | None = None) -> RowOut:
    data = {c: getattr(r, c) for c in RowOut.model_fields if c != "worker_name" and hasattr(r, c)}
    return RowOut(**data, worker_name=name)


async def list_rows(ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID) -> list[RowOut]:  # ruff: ignore[unused-function-argument]
    await load_version(db, version_id)
    rows = await version_rows(db, version_id)
    names = await worker_names(db, [r.candidate_id for r in rows])
    return [row_out(r, names.get(r.candidate_id)) for r in rows]


async def list_items(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    version_id: uuid.UUID,
    row_id: uuid.UUID,
) -> list[ItemOut]:
    await load_version(db, version_id)
    rows = await db.execute(
        select(PayrollExportItem)
        .where(PayrollExportItem.version_id == version_id, PayrollExportItem.row_id == row_id)
        .order_by(PayrollExportItem.item_date, PayrollExportItem.source_kind, PayrollExportItem.id)
    )
    items = list(rows.scalars())
    if not items and (await db.get(PayrollExportRow, row_id)) is None:
        raise NotFound("Payroll export row not found")
    return [
        ItemOut(
            id=i.id,
            source_kind=i.source_kind,
            source_id=i.source_id,
            placement_id=i.placement_id,
            bucket=i.bucket,
            category=i.category,
            item_date=i.item_date,
            quantity=i.quantity,
            rate=i.rate,
            multiplier=i.multiplier,
            amount=i.amount,
            currency=i.currency,
        )
        for i in items
    ]


# ================================================================ exceptions
def _exceptions_out(period_id: uuid.UUID, c: Collected) -> ExceptionsOut:
    lists = {k: [ExceptionItem(**e) for e in c.exceptions.get(k, [])] for k in EXCEPTION_KINDS}
    return ExceptionsOut(pay_period_id=period_id, blocking=c.blocking, counts=c.counts(), **lists)


async def _has_final(db: AsyncSession, period_id: uuid.UUID) -> bool:
    n = (
        await db.execute(
            select(func.count())
            .select_from(PayrollExportVersion)
            .where(PayrollExportVersion.pay_period_id == period_id, PayrollExportVersion.kind == "final")
        )
    ).scalar_one()
    return bool(n)


async def exceptions(ctx: RequestContext, db: AsyncSession, period_id: uuid.UUID) -> ExceptionsOut:
    period = await load_period(db, period_id)
    c = await collect(
        ctx, db, period, supplementary=await _has_final(db, period.id), exclude_exceptions=False
    )
    return _exceptions_out(period.id, c)


# ================================================================ write a version
async def _next_seq(db: AsyncSession, period_id: uuid.UUID) -> int:
    cur = (
        await db.execute(
            select(func.max(PayrollExportVersion.seq)).where(PayrollExportVersion.pay_period_id == period_id)
        )
    ).scalar_one()
    return int(cur or 0) + 1


async def _worker_human_ids(db: AsyncSession, cids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not cids:
        return {}
    out = dict(
        (await db.execute(select(Candidate.id, Candidate.human_id).where(Candidate.id.in_(list(cids)))))
        .tuples()
        .all()
    )
    wp = await db.execute(
        select(WorkerProfile.candidate_id, WorkerProfile.worker_human_id).where(
            WorkerProfile.candidate_id.in_(list(cids))
        )
    )
    out.update({c: h for c, h in wp.tuples().all() if h})
    return {k: str(v) for k, v in out.items() if v}


def _content_payload(
    v: PayrollExportVersion, rows: Sequence[PayrollExportRow], items: Sequence[PayrollExportItem]
) -> dict[str, Any]:
    row_cols = [c.name for c in PayrollExportRow.__table__.columns if c.name not in ("tenant_id",)]
    item_cols = [c.name for c in PayrollExportItem.__table__.columns if c.name not in ("tenant_id",)]
    return {
        "version": {
            "id": v.id,
            "pay_period_id": v.pay_period_id,
            "legal_entity_id": v.legal_entity_id,
            "kind": v.kind,
            "seq": v.seq,
            "items_hash": v.items_hash,
            "totals": v.totals,
        },
        "rows": sorted(([getattr(r, c) for c in row_cols] for r in rows), key=lambda x: str(x[0])),
        "items": sorted(([getattr(i, c) for c in item_cols] for i in items), key=lambda x: str(x[0])),
    }


async def _write_version(
    ctx: RequestContext,
    db: AsyncSession,
    period: PayPeriod,
    collected: Collected,
    *,
    kind: str,
    supplementary: bool,
    exclude_exceptions: bool,
    based_on: uuid.UUID | None = None,
) -> tuple[PayrollExportVersion, list[PayrollExportRow], list[tuple[PricedItem, PayrollExportItem]]]:
    rows_t = earnings.build_rows(collected.items)
    v = PayrollExportVersion(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, "PEX", width=6),
        pay_period_id=period.id,
        legal_entity_id=period.legal_entity_id,
        kind=kind,
        supplementary=supplementary,
        seq=await _next_seq(db, period.id),
        status="building",
        based_on_preview_id=based_on,
        exclude_exceptions=exclude_exceptions,
        generated_by=user_of(ctx),
        generated_at=now(),
        items_hash=earnings.items_hash(collected.items),
        exceptions={k: e for k, e in collected.exceptions.items() if e},
        totals=earnings.totals_by_currency(rows_t),
        row_count=len(rows_t),
        item_count=len(collected.items),
        provider_status="not_sent",
    )
    db.add(v)
    await db.flush()
    hids = await _worker_human_ids(db, [r.candidate_id for r in rows_t])
    rows: list[PayrollExportRow] = []
    pairs: list[tuple[PricedItem, PayrollExportItem]] = []
    for rt in rows_t:
        ident = collected.identities.get(rt.candidate_id)
        row = PayrollExportRow(
            id=uuid.uuid4(),
            tenant_id=v.tenant_id,
            version_id=v.id,
            candidate_id=rt.candidate_id,
            worker_human_id=hids.get(rt.candidate_id),
            legal_entity_id=period.legal_entity_id,
            placement_ids=list(rt.placement_ids),
            worker_type=collected.worker_types.get(rt.candidate_id),
            pan_last4=getattr(ident, "pan_last4", None),
            bank_last4=getattr(ident, "bank_account_last4", None),
            ifsc=getattr(ident, "ifsc", None),
            currency=rt.currency,
            hours_regular=rt.hours["regular"],
            hours_ot=rt.hours["ot"],
            hours_double_time=rt.hours["double_time"],
            hours_holiday=rt.hours["holiday"],
            hours_leave_paid=rt.hours["leave_paid"],
            hours_leave_unpaid=rt.hours["leave_unpaid"],
            days_leave_paid=rt.leave_days["leave_paid"],
            days_leave_unpaid=rt.leave_days["leave_unpaid"],
            earnings_regular=rt.earnings["regular"],
            earnings_ot=rt.earnings["ot"],
            earnings_double_time=rt.earnings["double_time"],
            earnings_holiday=rt.earnings["holiday"],
            earnings_leave_paid=rt.earnings["leave_paid"],
            earnings_base=rt.earnings["base"],
            pay_rates=dict(rt.pay_rates),
            reimbursements_total=rt.reimbursements_total,
            adjustments_total=rt.adjustments_total,
            rate_adjustments_total=rt.rate_adjustments_total,
            gross_earnings_inputs=rt.gross_earnings_inputs,
        )
        rows.append(row)
        for it in rt.items:
            pairs.append(
                (
                    it,
                    PayrollExportItem(
                        id=uuid.uuid4(),
                        tenant_id=v.tenant_id,
                        version_id=v.id,
                        row_id=row.id,
                        source_kind=it.source_kind,
                        source_id=it.source_id,
                        placement_id=it.placement_id,
                        rate_segment_id=it.rate_segment_id,
                        bucket=it.bucket,
                        category=it.category,
                        item_date=it.item_date,
                        quantity=it.quantity,
                        rate=it.rate,
                        multiplier=it.multiplier,
                        amount=it.amount,
                        currency=it.currency,
                    ),
                )
            )
    db.add_all(rows)
    await db.flush()
    db.add_all([p[1] for p in pairs])
    await db.flush()
    return v, rows, pairs


# ================================================================ preview
async def generate_preview(
    ctx: RequestContext, db: AsyncSession, period_id: uuid.UUID, data: PreviewIn
) -> VersionOut:
    period = await load_period(db, period_id, lock=True)
    supplementary = await _has_final(db, period.id)
    c = await collect(
        ctx, db, period, supplementary=supplementary, exclude_exceptions=data.exclude_exceptions
    )
    if c.blocking and not data.exclude_exceptions:
        raise ConflictError(
            "Resolve the payroll exceptions or proceed excluding them",
            code="payroll_exceptions",
            details={"counts": c.counts()},
        )
    if supplementary and not c.items:
        raise ConflictError("There is nothing new to export for this period", code="nothing_to_export")
    older = (
        await db.execute(
            select(PayrollExportVersion).where(
                PayrollExportVersion.pay_period_id == period.id,
                PayrollExportVersion.kind == "preview",
                PayrollExportVersion.status == "generated",
            )
        )
    ).scalars()
    for o in older:
        o.status = "superseded"
    v, _, _ = await _write_version(
        ctx,
        db,
        period,
        c,
        kind="preview",
        supplementary=supplementary,
        exclude_exceptions=data.exclude_exceptions,
    )
    v.status = "generated"
    await db.flush()
    emit(db, PayrollPreviewGenerated(tenant_id=v.tenant_id, version_id=v.id, pay_period_id=period.id))
    return version_out(v)


async def delete_preview(ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID) -> None:  # ruff: ignore[unused-function-argument]
    v = await load_version(db, version_id, lock=True)
    if v.kind != "preview":
        raise ConflictError("Only a preview can be deleted", code="document_immutable")
    await db.execute(delete(PayrollExportItem).where(PayrollExportItem.version_id == v.id))
    await db.execute(delete(PayrollExportRow).where(PayrollExportRow.version_id == v.id))
    await db.delete(v)
    await db.flush()


# ================================================================ finalize
async def finalize(ctx: RequestContext, db: AsyncSession, preview_id: uuid.UUID) -> VersionOut:
    require_human(actor_from_ctx(ctx))
    pv = await load_version(db, preview_id, lock=True)
    if pv.kind != "preview":
        raise ConflictError("Only a preview can be finalized", code="not_a_preview")
    if pv.status != "generated":
        raise ConflictError("This preview is no longer current; regenerate it", code="preview_stale")
    period = await load_period(db, pv.pay_period_id, lock=True)
    has_final = await _has_final(db, period.id)
    if not pv.supplementary and period.status == "locked":
        raise ConflictError("The pay period is locked", code="pay_period_locked")
    if pv.supplementary != has_final:
        raise ConflictError("This preview is no longer current; regenerate it", code="preview_stale")
    c = await collect(ctx, db, period, supplementary=has_final, exclude_exceptions=pv.exclude_exceptions)
    if not c.items:
        raise ConflictError("There is nothing to export for this period", code="nothing_to_export")
    if (c.blocking and not pv.exclude_exceptions) or earnings.items_hash(c.items) != pv.items_hash:
        raise ConflictError("Approved items changed since the preview; regenerate it", code="preview_stale")
    kind = "supplementary" if has_final else "final"
    v, rows, pairs = await _write_version(
        ctx,
        db,
        period,
        c,
        kind=kind,
        supplementary=has_final,
        exclude_exceptions=pv.exclude_exceptions,
        based_on=pv.id,
    )
    await postings.record_posted(
        ctx,
        db,
        "payable",
        [
            postings.PostingLine(
                source_kind=it.source_kind,
                source_id=it.source_id,  # type: ignore[arg-type]
                target_kind=TARGET_KIND,
                target_id=row.row_id,
                document_id=v.id,
                quantity=Decimal(row.quantity),
                amount=Decimal(row.amount),
                currency=row.currency,
                placement_id=row.placement_id,
                payee_kind="worker",
            )
            for it, row in pairs
            if it.postable
        ],
    )
    v.content_hash = earnings.canonical_hash(_content_payload(v, rows, [p[1] for p in pairs]))
    v.status = "finalized"
    v.finalized_by, v.finalized_at = user_of(ctx), now()
    pv.status = "superseded"
    was_open = period.status == "open"
    if was_open:
        period.status = "locked"
        period.locked_at = now()
        period.locked_by = user_of(ctx)
    await db.flush()
    audit.record(
        "payroll.finalized",
        "payroll_export_version",
        v.id,
        session=db,
        kind=kind,
        items=v.item_count,
        pay_period_id=str(period.id),
    )
    if was_open:
        emit(
            db,
            PayPeriodLocked(
                tenant_id=period.tenant_id, pay_period_id=period.id, legal_entity_id=period.legal_entity_id
            ),
        )
    emit(
        db,
        PayrollExportFinalized(
            tenant_id=v.tenant_id,
            export_version_id=v.id,
            pay_period_id=period.id,
            legal_entity_id=period.legal_entity_id,
        ),
    )
    await _queue_push(ctx, db, v)
    return version_out(v)


async def _queue_push(ctx: RequestContext, db: AsyncSession, v: PayrollExportVersion) -> None:
    """Non-manual providers get a ``connector_jobs`` row (key ``payroll:push:payroll_export_version:<id>:1``);
    the job runs after commit. Manual: nothing to push (status stays ``not_sent``)."""
    from app.services.connectors import registry

    row = await registry.active_config(db, "payroll", v.legal_entity_id)
    if row is None or row.provider_key == "manual":
        return
    if row.installation_id is not None:
        return  # Epic 71 (D-18): Installation configs are pushed by the outbox-triggered ``payroll_push`` sync job
    from app.services import connectors_service

    await connectors_service.enqueue(
        ctx,
        db,
        "payroll",
        "push",
        "payroll_export_version",
        v.id,
        1,
        {"legal_entity_id": str(v.legal_entity_id), "version_id": str(v.id), "kind": v.kind},
    )
    v.provider_status = "sent"
    await db.flush()


# ================================================================ mark processed
async def mark_processed(
    ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID, payroll_reference: str | None
) -> VersionOut:
    require_human(actor_from_ctx(ctx))
    v = await load_version(db, version_id, lock=True)
    if v.kind == "preview":
        raise ConflictError("Only a final or supplementary version can be processed", code="not_finalized")
    if v.status == "processed":
        return version_out(v)
    v.status = "processed"
    v.provider_status = "processed"
    if payroll_reference:
        v.provider_reference = payroll_reference
    v.processed_at, v.processed_by = now(), user_of(ctx)
    await db.flush()
    line_ids = list(
        (
            await db.execute(
                select(PayrollExportItem.source_id).where(
                    PayrollExportItem.version_id == v.id,
                    PayrollExportItem.source_kind.in_(list(earnings.EXPENSE_KINDS)),
                )
            )
        ).scalars()
    )
    await _mark_reimbursed(ctx, db, [i for i in line_ids if i is not None], v.id, payroll_reference)
    audit.record("payroll.processed", "payroll_export_version", v.id, session=db, reference=payroll_reference)
    emit(db, PayrollExportProcessed(tenant_id=v.tenant_id, version_id=v.id))
    return version_out(v)


async def _mark_reimbursed(
    ctx: RequestContext, db: AsyncSession, line_ids: list[uuid.UUID], version_id: uuid.UUID, ref: str | None
) -> None:
    if not line_ids:
        return
    try:
        from app.services import expenses_service
    except ImportError:  # pragma: no cover - Epic 27 always present after B4
        return
    mark = getattr(expenses_service, "mark_reimbursed", None)
    if mark is None:  # pragma: no cover
        logger.warning("expenses_service.mark_reimbursed missing; claims not marked")
        return
    await mark(ctx, db, line_ids, version_id, ref)
