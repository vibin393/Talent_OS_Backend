# ruff: noqa: E501, ARG001
"""Invoice Runs (FR-145, AD-23): pull approved, unposted items through ``postings.load_unposted``, group them per
Billing Rule and currency, price them (``pricing``), preview tax (``tax``) and insert one draft per group, each
in its own savepoint with ``postings.reserve`` (a concurrently reserved item is skipped with an exception).

Negative ``adjustment_entry`` items and every ``rate_adjustment`` go to ``corrections`` (Credit / Debit Notes on
the original Invoice). A second building run for the same GST registration is refused (409 ``run_in_progress``,
``pg_advisory_xact_lock``).
"""

import dataclasses
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, fiscal
from app.core.context import RequestContext
from app.core.errors import ConflictError, DomainError, ValidationFailed
from app.core.events import emit
from app.core.money import MONEY_CONTEXT, quantize_amount, quantize_hours
from app.events.billing import InvoiceRunCompleted
from app.models.billing import BillingRule, BillingScheduleItem, Invoice, InvoiceRun, InvoiceRunException
from app.models.placements import Placement
from app.schemas.billing import RunCreate, RunDetailOut, RunExceptionOut, RunOut
from app.services import commercials_service, legal_entities_service, placements_service, postings
from app.services.billing import context, corrections, invoices, pricing, rates
from app.services.billing.common import conflict, invalid, settings, tenant_of, user_id

BILLABLE_STATUSES = ("active", "offboarding", "ended")
CORRECTION_KINDS = ("rate_adjustment",)
PO_EXCEPTIONS = {"po_missing", "po_expired", "po_exhausted", "po_not_started", "po_closed", "po_cancelled"}


@dataclasses.dataclass(frozen=True, slots=True)
class InvoiceRunQueued:
    """Domain-local (not in the event catalogue): enqueue the build after commit."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID


def _out(run: InvoiceRun) -> RunOut:
    return RunOut.model_validate(run)


async def _human_id(db: AsyncSession) -> str:
    count = (await db.execute(select(func.count(InvoiceRun.id)))).scalar() or 0
    return f"RUN-{int(count) + 1:05d}"


async def create_run(ctx: RequestContext, db: AsyncSession, data: RunCreate) -> RunOut:
    if data.period_end < data.period_start:
        raise invalid("period_invalid", "The period end is before its start")
    await legal_entities_service.assert_accepts_new_documents(ctx, db, data.legal_entity_id)
    reg = data.gst_registration_id
    if reg is None:
        reg = await legal_entities_service.default_registration_for(ctx, db, data.legal_entity_id, None)
    key = f"billing_run:{reg or data.legal_entity_id}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
    busy = (
        await db.execute(
            select(InvoiceRun.id).where(
                InvoiceRun.legal_entity_id == data.legal_entity_id,
                InvoiceRun.gst_registration_id.is_(None)
                if reg is None
                else InvoiceRun.gst_registration_id == reg,
                InvoiceRun.status.in_(("queued", "building")),
            )
        )
    ).first()
    if busy is not None:
        raise conflict(
            "run_in_progress",
            "An invoice run is already building for this GST registration",
            run_id=str(busy[0]),
        )
    run = InvoiceRun(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await _human_id(db),
        legal_entity_id=data.legal_entity_id,
        gst_registration_id=reg,
        period_start=data.period_start,
        period_end=data.period_end,
        client_id=data.client_id,
        placement_ids=list(data.placement_ids) if data.placement_ids else None,
        status="queued",
        created_by=user_id(ctx),
    )
    db.add(run)
    await db.flush()
    await db.refresh(run)
    emit(db, InvoiceRunQueued(tenant_id=run.tenant_id, run_id=run.id))
    audit.record("invoice_run.created", "invoice_run", run.id, session=db)
    return _out(run)


async def get_run(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> RunDetailOut:
    run = (await db.execute(select(InvoiceRun).where(InvoiceRun.id == run_id))).scalar_one_or_none()
    if run is None:
        from app.services.billing.common import not_found

        raise not_found("invoice run")
    exc_rows = await db.execute(
        select(InvoiceRunException)
        .where(InvoiceRunException.run_id == run_id)
        .order_by(InvoiceRunException.kind)
    )
    drafts = await db.execute(
        select(Invoice).where(Invoice.invoice_run_id == run_id).order_by(Invoice.created_at)
    )
    data = _out(run).model_dump()
    return RunDetailOut(
        **data,
        exceptions=[RunExceptionOut.model_validate(e) for e in exc_rows.scalars()],
        drafts=[invoices.summary("invoice", d) for d in drafts.scalars()],
    )


async def list_runs(ctx: RequestContext, db: AsyncSession, limit: int = 100) -> list[RunOut]:
    rows = await db.execute(select(InvoiceRun).order_by(InvoiceRun.created_at.desc()).limit(limit))
    return [_out(r) for r in rows.scalars()]


async def discard_run(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> RunDetailOut:
    run = (
        await db.execute(select(InvoiceRun).where(InvoiceRun.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if run is None:
        from app.services.billing.common import not_found

        raise not_found("invoice run")
    if run.status in ("queued", "building"):
        raise conflict("run_in_progress", "The run is still building")
    ids = (
        await db.execute(
            select(Invoice.id).where(
                Invoice.invoice_run_id == run_id, Invoice.status.in_(("draft", "pending_approval"))
            )
        )
    ).scalars()
    for inv_id in list(ids):
        await invoices.discard(ctx, db, inv_id)
    run.status = "discarded"
    await db.flush()
    return await get_run(ctx, db, run_id)


# ================================================================ build
class _Build:
    def __init__(self, ctx: RequestContext, db: AsyncSession, run: InvoiceRun) -> None:
        self.ctx, self.db, self.run = ctx, db, run
        self.exceptions: list[InvoiceRunException] = []
        self.seen: set[tuple[Any, str]] = set()
        self.tid = tenant_of(db)
        self.bill_ctx: dict[tuple[uuid.UUID, date], Any] = {}
        self.inputs: dict[uuid.UUID, Any] = {}

    def exc(
        self,
        kind: str,
        placement_id: uuid.UUID | None = None,
        client_id: uuid.UUID | None = None,
        *,
        severity: str = "error",
        **details: Any,
    ) -> None:
        key = (placement_id or client_id, kind)
        if key in self.seen:
            return
        self.seen.add(key)
        self.exceptions.append(
            InvoiceRunException(
                id=uuid.uuid4(),
                tenant_id=self.tid,
                run_id=self.run.id,
                placement_id=placement_id,
                client_id=client_id,
                kind=kind,
                severity=severity,
                details={
                    k: (str(v) if not isinstance(v, (int, float, str, bool, type(None), list, dict)) else v)
                    for k, v in details.items()
                },
            )
        )

    async def billing_context(self, placement_id: uuid.UUID, on: date) -> Any:
        key = (placement_id, on)
        if key not in self.bill_ctx:
            try:
                self.bill_ctx[key] = await placements_service.billing_context(
                    self.ctx, self.db, placement_id, on
                )
            except DomainError:
                self.bill_ctx[key] = None
        return self.bill_ctx[key]

    async def multipliers(self, placement_id: uuid.UUID) -> tuple[dict[str, Decimal], Decimal | None]:
        if placement_id not in self.inputs:
            try:
                from app.services import time_service

                bi = await time_service.billing_inputs(
                    self.ctx, self.db, placement_id, self.run.period_start, self.run.period_end
                )
                self.inputs[placement_id] = (
                    {k: Decimal(v) for k, v in bi.bill_multipliers.items()},
                    Decimal(bi.hours_per_day),
                )
            except Exception:
                self.inputs[placement_id] = ({}, None)
        return self.inputs[placement_id]  # type: ignore[no-any-return]


def _desc(placement: Placement, rule: BillingRule, category: str, start: date, end: date) -> str:
    if rule.line_template:
        return rule.line_template.replace("{category}", category).replace(
            "{period}", f"{start:%d %b} - {end:%d %b %Y}"
        )[:500]
    role = placement.role_title or "Contract staffing services"
    label = "" if category == "regular" else f" ({category.replace('_', ' ')})"
    return f"{role}{label} - {placement.human_id} - {start:%d %b} to {end:%d %b %Y}"[:500]


async def _monthly_items(b: _Build, placement: Placement, rule: BillingRule) -> None:
    """Generate the monthly-fixed schedule items of the run's months (idempotent)."""
    year, month = b.run.period_start.year, b.run.period_start.month
    while (year, month) <= (b.run.period_end.year, b.run.period_end.month):
        first, last = fiscal.month_bounds(year, month)
        start = max(first, placement.actual_start_date or first)
        end = min(last, placement.end_date or last)
        year, month = fiscal.add_months(year, month, 1)
        if end < start or end > b.run.period_end:
            continue
        exists = (
            await b.db.execute(
                select(BillingScheduleItem.id).where(
                    BillingScheduleItem.placement_id == placement.id,
                    BillingScheduleItem.kind == "monthly_fixed",
                    BillingScheduleItem.period_start == start,
                )
            )
        ).first()
        if exists is not None:
            continue
        bc = await b.billing_context(placement.id, start)
        if bc is None or bc.bill_rate is None:
            b.exc("rate_card_exception_unapproved", placement.id, placement.client_id, reason="no_bill_rate")
            continue
        factor = Decimal(1)
        if rule.prorate and (start != first or end != last):
            try:
                from app.services import time_service

                factor = Decimal(
                    (
                        await time_service.billing_inputs(b.ctx, b.db, placement.id, start, end)
                    ).proration_factor
                )
            except Exception:
                factor = MONEY_CONTEXT.divide(
                    Decimal((end - start).days + 1), Decimal((last - first).days + 1)
                )
        amount = pricing.prorate_factor(Decimal(bc.bill_rate), factor, placement.currency)
        b.db.add(
            BillingScheduleItem(
                id=uuid.uuid4(),
                tenant_id=b.tid,
                placement_id=placement.id,
                kind="monthly_fixed",
                period_start=start,
                period_end=end,
                amount=amount,
                currency=placement.currency,
                description=f"{placement.role_title or 'Monthly services'} - {start:%b %Y}",
            )
        )
    await b.db.flush()


async def _milestone_clients(
    db: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[uuid.UUID, str, uuid.UUID]]:
    if not ids:
        return {}
    from app.models.commercials import Contract, ContractMilestone

    rows = await db.execute(
        select(ContractMilestone.id, Contract.client_id, ContractMilestone.name, Contract.id)
        .join(Contract, Contract.id == ContractMilestone.contract_id)
        .where(ContractMilestone.id.in_(ids))
    )
    return {r[0]: (r[1], r[2], r[3]) for r in rows.all() if r[1] is not None}


async def build_run(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> RunOut:
    run = (
        await db.execute(select(InvoiceRun).where(InvoiceRun.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if run is None or run.status not in ("queued", "building"):
        return _out(run) if run is not None else None  # type: ignore[return-value]
    run.status = "building"
    run.progress_pct = 5
    await db.flush()
    b = _Build(ctx, db, run)
    await rates.ensure_tax_master(db)
    cfg = await settings(db)
    today = await invoices.today_for_entity(ctx, db, run.legal_entity_id)

    stmt = select(Placement).where(
        Placement.legal_entity_id == run.legal_entity_id, Placement.status.in_(BILLABLE_STATUSES)
    )
    if run.gst_registration_id is not None:
        stmt = stmt.where(Placement.supplier_gst_registration_id == run.gst_registration_id)
    if run.client_id is not None:
        stmt = stmt.where(Placement.client_id == run.client_id)
    if run.placement_ids:
        stmt = stmt.where(Placement.id.in_(list(run.placement_ids)))
    placements = {p.id: p for p in (await db.execute(stmt.order_by(Placement.human_id))).scalars()}
    rules = {
        r.placement_id: r
        for r in (
            await db.execute(
                select(BillingRule).where(BillingRule.placement_id.in_(list(placements) or [uuid.uuid4()]))
            )
        ).scalars()
    }
    for p in placements.values():
        rule = rules.get(p.id)
        if rule is not None and rule.basis == "monthly_fixed":
            await _monthly_items(b, p, rule)

    items = await postings.load_unposted(
        ctx,
        db,
        postings.SourceQuery(
            ledger="billing",
            period_end=run.period_end,
            placement_ids=list(placements),
            legal_entity_id=run.legal_entity_id,
        ),
    )
    if not run.placement_ids:  # placement-less items (manual, milestones) of the Legal Entity
        extra = await postings.load_unposted(
            ctx,
            db,
            postings.SourceQuery(
                ledger="billing", period_end=run.period_end, legal_entity_id=run.legal_entity_id
            ),
        )
        have = {(i.source_kind, i.source_id) for i in items}
        items += [i for i in extra if i.placement_id is None and (i.source_kind, i.source_id) not in have]
    run.progress_pct = 30
    milestone_info = await _milestone_clients(
        db, [i.source_id for i in items if i.source_kind == "milestone"]
    )

    correction_items: list[postings.SourceItem] = []
    # group key -> (client_id, currency, placement_id | None, po_id | None, contract_id) -> list of (DraftLine)
    groups: dict[tuple[Any, ...], dict[tuple[Any, ...], invoices.DraftLine]] = defaultdict(dict)
    stale_before = today - timedelta(days=int(cfg.stale_unbilled_days))
    for item in items:
        attrs = dict(item.attributes or {})
        if attrs.get("billable") is False:
            continue
        if item.source_kind in CORRECTION_KINDS or (
            item.source_kind == "adjustment_entry" and item.quantity < 0
        ):
            correction_items.append(item)
            continue
        placement = placements.get(item.placement_id) if item.placement_id else None
        if item.placement_id is not None and placement is None:
            continue  # another registration / filtered out
        if placement is not None:
            client_id, currency, contract_id = placement.client_id, placement.currency, placement.contract_id
            rule = rules.get(placement.id)
            if rule is None:
                b.exc("billing_rule_missing", placement.id, client_id)
                continue
            if item.currency and item.currency.upper() != currency:
                b.exc(
                    "mixed_currency", placement.id, client_id, item_currency=item.currency, currency=currency
                )
                continue
        else:
            client_raw = attrs.get("client_id")
            contract_id = None
            if item.source_kind == "milestone" and item.source_id in milestone_info:
                client_raw, _name, contract_id = milestone_info[item.source_id]
            if client_raw is None:
                continue
            client_id = client_raw if isinstance(client_raw, uuid.UUID) else uuid.UUID(str(client_raw))
            currency, rule = item.currency.upper(), None
        if run.client_id is not None and client_id != run.client_id:
            continue
        if item.item_date < stale_before:
            b.exc(
                "stale_unbilled",
                placement.id if placement else None,
                client_id,
                severity="warning",
                oldest=item.item_date.isoformat(),
            )
        po_id = (
            placement.po_id
            if placement is not None
            else (uuid.UUID(str(attrs["po_id"])) if attrs.get("po_id") else None)
        )
        grouping = rule.grouping if rule is not None else "per_placement"
        gkey = (
            client_id,
            currency,
            placement.id if (placement and grouping == "per_placement") else None,
            po_id if grouping == "per_po" else None,
            contract_id,
        )
        line = await _line_for(b, item, attrs, placement, rule, currency, po_id, milestone_info)
        if line is None:
            continue
        lkey = (
            line.line_kind,
            line.placement_id,
            line.category,
            line.rate,
            line.multiplier,
            line.unit,
            line.pure_agent,
            line.po_id,
            line.description if line.line_kind not in ("time", "adjustment") else "",
        )
        existing = groups[gkey].get(lkey)
        if existing is None:
            groups[gkey][lkey] = line
        else:
            existing.quantity = (
                quantize_hours(existing.quantity + line.quantity)
                if existing.unit in ("hour", "day")
                else existing.quantity + line.quantity
            )
            existing.sources.extend(line.sources)
            existing.period_start = (
                min(d for d in (existing.period_start, line.period_start) if d)
                if (existing.period_start or line.period_start)
                else None
            )
            existing.period_end = (
                max(d for d in (existing.period_end, line.period_end) if d)
                if (existing.period_end or line.period_end)
                else None
            )
    run.progress_pct = 60

    checked: dict[uuid.UUID, bool] = {}
    drafts = 0
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for (client_id, currency, placement_id, po_id, contract_id), lines in groups.items():
        placement = placements.get(placement_id) if placement_id else None
        if not await _group_ok(b, client_id, placement, po_id, currency, today, checked):
            continue
        draft_lines = list(lines.values())
        for dl in draft_lines:
            if (
                dl.line_kind in ("time", "adjustment")
                and dl.period_start
                and dl.period_end
                and placement is not None
            ):
                rule = rules[placement.id]
                dl.description = _desc(
                    placement, rule, dl.category or "regular", dl.period_start, dl.period_end
                )
        try:
            async with db.begin_nested():
                inv = await invoices.create_draft(
                    ctx,
                    db,
                    legal_entity_id=run.legal_entity_id,
                    gst_registration_id=run.gst_registration_id,
                    client_id=client_id,
                    contract_id=contract_id,
                    currency=currency,
                    lines=draft_lines,
                    run_id=run.id,
                )
            drafts += 1
            totals[currency] += Decimal(inv.document_total)
        except ConflictError as exc:
            b.exc(
                "item_already_reserved" if exc.code == "source_already_posted" else exc.code,
                placement_id,
                client_id,
                message=str(exc),
            )
        except ValidationFailed as exc:
            b.exc(exc.code, placement_id, client_id, message=str(exc))
    run.progress_pct = 85
    if correction_items:
        try:
            async with db.begin_nested():
                for kind, pid, client_id, message in await corrections.build(
                    ctx, db, correction_items, run_id=run.id
                ):
                    b.exc(kind, pid, client_id, severity="warning", message=message)
        except DomainError as exc:
            b.exc(exc.code, None, None, message=str(exc))
    for e in b.exceptions:
        db.add(e)
    run.status = "built"
    run.progress_pct = 100
    run.draft_count = drafts
    run.totals = {k: str(quantize_amount(v, k)) for k, v in totals.items()}
    await db.flush()
    emit(
        db,
        InvoiceRunCompleted(
            tenant_id=run.tenant_id,
            run_id=run.id,
            legal_entity_id=run.legal_entity_id,
            draft_count=drafts,
            exception_count=sum(1 for e in b.exceptions if e.severity == "error"),
        ),
    )
    logger.info("invoice run {} built: {} drafts, {} exceptions", run.human_id, drafts, len(b.exceptions))
    return _out(run)


async def _group_ok(
    b: _Build,
    client_id: uuid.UUID,
    placement: Placement | None,
    po_id: uuid.UUID | None,
    currency: str,
    today: date,
    checked: dict[uuid.UUID, bool],
) -> bool:
    pid = placement.id if placement else None
    if client_id not in checked:
        try:
            await context.supply_for(
                b.ctx,
                b.db,
                legal_entity_id=b.run.legal_entity_id,
                gst_registration_id=b.run.gst_registration_id,
                client_id=client_id,
                contract_id=placement.contract_id if placement else None,
                doc_date=today,
            )
            checked[client_id] = True
        except ValidationFailed as exc:
            b.exc(exc.code, pid, client_id, message=str(exc))
            checked[client_id] = False
    if not checked[client_id]:
        if pid is not None:
            b.exc(next(e.kind for e in b.exceptions if e.client_id == client_id), pid, client_id)
        return False
    if currency != "INR":
        try:
            await context.fx_for(b.ctx, b.db, currency, today)
        except ValidationFailed as exc:
            b.exc(exc.code, pid, client_id, currency=currency)
            return False
    try:
        profile = await commercials_service.get_billing_profile(b.ctx, b.db, client_id)
    except DomainError:
        return False
    if po_id is None and profile.po_required:
        b.exc("po_missing", pid, client_id)
        return False
    if po_id is not None:
        validity = await commercials_service.validate_po(
            b.ctx, b.db, po_id, today, period_end=b.run.period_end
        )
        if not validity.valid and validity.reason in PO_EXCEPTIONS:
            b.exc(validity.reason, pid, client_id, po_id=str(po_id))
            return False
    return True


async def _line_for(
    b: _Build,
    item: postings.SourceItem,
    attrs: dict[str, Any],
    placement: Placement | None,
    rule: BillingRule | None,
    currency: str,
    po_id: uuid.UUID | None,
    milestone_info: dict[uuid.UUID, tuple[uuid.UUID, str, uuid.UUID]],
) -> invoices.DraftLine | None:
    kind = item.source_kind
    sac = rule.sac_code if rule is not None else None
    if kind == "expense_line":
        pure = attrs.get("rebill_tax_mode") == "pure_agent" or (
            rule is not None and rule.expense_rebill == "pure_agent"
        )
        amount = Decimal(item.amount or 0)
        lk = "pure_agent_expense" if pure else "expense"
        return invoices.DraftLine(
            line_kind=lk,
            description=f"Reimbursable expense - {attrs.get('category') or 'expense'} ({item.item_date:%d %b %Y})",
            sac_code=await rates.default_sac(b.db, lk),
            quantity=Decimal(1),
            rate=amount,
            pure_agent=pure,
            placement_id=item.placement_id,
            po_id=po_id,
            category=str(attrs.get("category") or "expense"),
            period_start=item.item_date,
            period_end=item.item_date,
            sources=[item],
        )
    if (
        item.amount is not None
        and attrs.get("unit") in (None, "item", "month")
        and kind != "adjustment_entry"
    ):
        lk = {"schedule_item": "schedule_fee", "milestone": "milestone"}.get(kind, "manual")
        if kind == "schedule_item":
            lk = {"conversion_fee": "conversion_fee", "monthly_fixed": "schedule_fee"}.get(
                str(attrs.get("category")), "schedule_fee"
            )
        elif kind == "manual_item":
            lk = {"placement_fee": "placement_fee", "milestone": "milestone"}.get(
                str(attrs.get("category")), "manual"
            )
        desc = str(attrs.get("description") or "")
        if kind == "milestone" and item.source_id in milestone_info:
            desc = f"Milestone: {milestone_info[item.source_id][1]}"
        qty = Decimal(attrs.get("quantity") or 1)
        rate = Decimal(attrs.get("rate") or item.amount)
        return invoices.DraftLine(
            line_kind=lk,
            description=desc or lk.replace("_", " ").capitalize(),
            sac_code=str(attrs.get("sac_code") or "") or await rates.default_sac(b.db, lk),
            quantity=qty,
            rate=rate,
            unit=str(attrs.get("unit") or "item"),
            placement_id=item.placement_id,
            po_id=po_id,
            category=str(attrs.get("category") or lk),
            period_start=attrs.get("period_start") or item.item_date,
            period_end=attrs.get("period_end") or item.item_date,
            sources=[item],
        )
    # hours (timesheet entries, positive adjustments and any hour-unit source)
    if placement is None or rule is None:
        return None
    category = str(attrs.get("category") or "regular")
    if rule.billable_categories and category not in rule.billable_categories:
        return None
    bc = await b.billing_context(placement.id, item.item_date)
    if bc is None or bc.bill_rate is None:
        b.exc(
            "rate_card_exception_unapproved",
            placement.id,
            placement.client_id,
            reason="no_bill_rate",
            date=item.item_date.isoformat(),
        )
        return None
    if rule.basis == "monthly_fixed" or bc.rate_unit == "month":
        return None  # billed by the monthly schedule item
    mults, hours_per_day = await b.multipliers(placement.id)
    multiplier = Decimal(attrs.get("multiplier") or mults.get(category) or Decimal(1))
    qty, unit = Decimal(item.quantity), "hour"
    if bc.rate_unit == "day" or rule.basis == "daily":
        unit = "day"
        qty = quantize_hours(MONEY_CONTEXT.divide(qty, hours_per_day or Decimal(8)))
        sources_qty = item
    else:
        sources_qty = item
    return invoices.DraftLine(
        line_kind="adjustment" if kind == "adjustment_entry" else "time",
        description="",
        sac_code=sac or await rates.default_sac(b.db, "time"),
        quantity=qty,
        rate=Decimal(bc.bill_rate),
        multiplier=multiplier,
        unit=unit,
        placement_id=placement.id,
        po_id=po_id,
        category=category,
        period_start=item.item_date,
        period_end=item.item_date,
        sources=[sources_qty],
    )
