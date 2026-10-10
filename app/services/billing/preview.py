# ruff: noqa: E501
"""Side-effect-free invoice run preview (Epic 63 A3, additive; FR-313). ``preview_run`` answers "what would
``runs.create_run`` + ``build_run`` produce now" without writing anything: the same ``postings.load_unposted`` items,
grouping, line pricing (``runs._line_for``) and group checks (``runs._group_ok``: supply / FX / billing profile / PO),
plus the time not yet approved. No run, draft, reservation, schedule item or number is created (monthly-fixed
schedule items not generated yet are not previewed). ``digest`` (sha256 of the item ids and amounts) is the staleness
version of the Billing Agent's ``billing.prepare_run_draft`` Proposal.
"""

import dataclasses
import hashlib
import json
import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.money import line_taxable, quantize_amount
from app.models.billing import BillingRule
from app.models.placements import Placement
from app.schemas.billing import RunCreate, RunPreviewClient, RunPreviewException, RunPreviewOut
from app.services import legal_entities_service, postings
from app.services.billing import invoices, runs

CATEGORY = {
    "po_missing": "po_missing",
    "po_expired": "po_missing",
    "po_not_started": "po_missing",
    "po_closed": "po_missing",
    "po_cancelled": "po_missing",
    "po_exhausted": "po_cap_breach",
    "po_insufficient": "po_cap_breach",
    "rate_card_exception_unapproved": "rate_mismatch",
    "rate_mismatch": "rate_mismatch",
    "billing_rule_missing": "billing_rule_missing",
    "mixed_currency": "mixed_currency",
    "unapproved_time": "unapproved_time",
}


@dataclasses.dataclass
class _PreviewRun:
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    gst_registration_id: uuid.UUID | None
    period_start: Any
    period_end: Any
    client_id: uuid.UUID | None
    placement_ids: list[uuid.UUID] | None


async def preview_run(ctx: RequestContext, db: AsyncSession, data: RunCreate) -> RunPreviewOut:
    reg = data.gst_registration_id
    if reg is None:
        reg = await legal_entities_service.default_registration_for(ctx, db, data.legal_entity_id, None)
    run = _PreviewRun(uuid.uuid4(), data.legal_entity_id, reg, data.period_start, data.period_end, data.client_id, data.placement_ids)
    b = runs._Build(ctx, db, run)  # type: ignore[arg-type]
    today = await invoices.today_for_entity(ctx, db, run.legal_entity_id)
    stmt = select(Placement).where(Placement.legal_entity_id == run.legal_entity_id, Placement.status.in_(runs.BILLABLE_STATUSES))
    if reg is not None:
        stmt = stmt.where(Placement.supplier_gst_registration_id == reg)
    if run.client_id is not None:
        stmt = stmt.where(Placement.client_id == run.client_id)
    if run.placement_ids:
        stmt = stmt.where(Placement.id.in_(list(run.placement_ids)))
    placements = {p.id: p for p in (await db.execute(stmt.order_by(Placement.human_id))).scalars()}
    rules = {
        r.placement_id: r
        for r in (
            await db.execute(select(BillingRule).where(BillingRule.placement_id.in_(list(placements) or [uuid.uuid4()])))
        ).scalars()
    }
    items = await postings.load_unposted(
        ctx, db,
        postings.SourceQuery(ledger="billing", period_end=run.period_end, placement_ids=list(placements), legal_entity_id=run.legal_entity_id),
    )  # fmt: skip
    if not run.placement_ids:
        extra = await postings.load_unposted(
            ctx, db, postings.SourceQuery(ledger="billing", period_end=run.period_end, legal_entity_id=run.legal_entity_id)
        )
        have = {(i.source_kind, i.source_id) for i in items}
        items += [i for i in extra if i.placement_id is None and (i.source_kind, i.source_id) not in have]
    milestone_info = await runs._milestone_clients(db, [i.source_id for i in items if i.source_kind == "milestone"])
    groups: dict[tuple[Any, ...], list[invoices.DraftLine]] = defaultdict(list)
    digest_rows: list[list[str]] = []
    for item in items:
        attrs = dict(item.attributes or {})
        if attrs.get("billable") is False or item.source_kind in runs.CORRECTION_KINDS or (item.source_kind == "adjustment_entry" and item.quantity < 0):
            continue
        placement = placements.get(item.placement_id) if item.placement_id else None
        if item.placement_id is not None and placement is None:
            continue
        if placement is not None:
            client_id, currency, contract_id = placement.client_id, placement.currency, placement.contract_id
            rule = rules.get(placement.id)
            if rule is None:
                b.exc("billing_rule_missing", placement.id, client_id)
                continue
            if item.currency and item.currency.upper() != currency:
                b.exc("mixed_currency", placement.id, client_id, item_currency=item.currency, currency=currency)
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
        po_id = placement.po_id if placement is not None else (uuid.UUID(str(attrs["po_id"])) if attrs.get("po_id") else None)
        grouping = rule.grouping if rule is not None else "per_placement"
        gkey = (
            client_id, currency, placement.id if (placement and grouping == "per_placement") else None,
            po_id if grouping == "per_po" else None, contract_id,
        )  # fmt: skip
        line = await runs._line_for(b, item, attrs, placement, rule, currency, po_id, milestone_info)
        if line is None:
            continue
        groups[gkey].append(line)
        digest_rows.append([item.source_kind, str(item.source_id), str(item.quantity), str(item.amount)])
    per_client: dict[uuid.UUID, RunPreviewClient] = {}
    checked: dict[uuid.UUID, bool] = {}
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    client_totals: dict[uuid.UUID, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal(0)))
    for (client_id, currency, placement_id, po_id, _contract), lines in groups.items():
        placement = placements.get(placement_id) if placement_id else None
        entry = per_client.setdefault(client_id, RunPreviewClient(client_id=client_id, currency=currency))
        entry.items += len(lines)
        if not await runs._group_ok(b, client_id, placement, po_id, currency, today, checked):
            continue
        amount = sum((line_taxable(ln.quantity, ln.rate, ln.multiplier, currency) for ln in lines), Decimal(0))
        entry.drafts += 1
        totals[currency] += amount
        client_totals[client_id][currency] += amount
    # time not yet approved (FR-313 "unapproved time")
    from app.services import time_service

    pending = await time_service.unapproved_timesheets(ctx, db, run.legal_entity_id, run.period_start, run.period_end)
    for ts in pending:
        p = placements.get(ts.placement_id)
        if p is None or ts.kind != "regular":
            continue
        if run.client_id is not None and p.client_id != run.client_id:
            continue
        b.exc("unapproved_time", ts.placement_id, p.client_id, severity="warning", timesheet_id=str(ts.timesheet_id), status=ts.status)
    for e in b.exceptions:
        if e.client_id is None:
            continue
        entry = per_client.setdefault(e.client_id, RunPreviewClient(client_id=e.client_id))
        entry.exceptions.append(
            RunPreviewException(
                kind=e.kind, category=CATEGORY.get(e.kind, "other"),  # type: ignore[arg-type]
                placement_id=e.placement_id, severity=e.severity, details=dict(e.details or {}),
            )
        )  # fmt: skip
    from app.models.clients import Client

    names = dict(
        (await db.execute(select(Client.id, Client.name).where(Client.id.in_(list(per_client) or [uuid.uuid4()])))).all()
    )
    for cid, entry in per_client.items():
        entry.client_name = str(names[cid]) if cid in names else None
        ct = client_totals.get(cid, {})
        entry.totals = {k: str(quantize_amount(v, k)) for k, v in ct.items()}
        if entry.currency in ct:
            entry.total = quantize_amount(ct[entry.currency], entry.currency or "INR")
    digest = hashlib.sha256(json.dumps(sorted(digest_rows), separators=(",", ":")).encode()).hexdigest()
    clients = sorted(per_client.values(), key=lambda c: (c.client_name or "", str(c.client_id)))
    return RunPreviewOut(
        legal_entity_id=run.legal_entity_id, period_start=run.period_start, period_end=run.period_end, client_id=run.client_id,
        clients=clients, draft_count=sum(c.drafts for c in clients),
        totals={k: str(quantize_amount(v, k)) for k, v in totals.items()},
        exception_count=sum(len(c.exceptions) for c in clients), digest=digest,
    )  # fmt: skip


__all__ = ["CATEGORY", "preview_run"]
