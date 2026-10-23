# ruff: file-ignore[line-too-long, non-imperative-mood, unused-function-argument]
"""Payment suggestions (Epic 71, story 71.6, spec A9).

* ``pull_payments`` sync job (Zoho Books): each ``LedgerPaymentDTO`` not yet suggested and not linked to a Talent OS
  Payment becomes a ``pending`` suggestion matched to an invoice by its ledger link, else by number within the Legal
  Entity (``match_note`` ``invoice_not_found`` / ``currency_mismatch`` / ``over_payment``); stale ones are swept.
* ``confirm`` (★ human only; ``ledger:sync`` + ``payment:record``): records the Payment through the P2 receivables
  service **as the confirming User**, links it to the ledger payment (the echo rule) and marks the suggestion
  ``confirmed``. Receivables errors (closed period, party mismatch) are returned unchanged.
* ``reject`` (human, reason). Nothing is recorded from provider data without a person.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.events import emit
from app.core.pagination import Page
from app.core.permissions import Principal
from app.events.ledger_sync import PaymentSuggestionDecided
from app.models.billing import Invoice
from app.models.clients import Client
from app.models.connectors import ProviderConnectorConfig
from app.models.ledger_sync import PaymentSuggestion
from app.models.receivables import PAYMENT_MODES, InvoiceBalance
from app.schemas.ledger_sync import PaymentSuggestionOut
from app.schemas.receivables import PaymentIn
from app.services import integrations_service, receivables_service
from app.services.ledger_sync.common import get_settings_row, notify, now, tenant_of

__all__ = ["confirm", "list_suggestions", "prefill", "pull_payments", "reject", "sweep_stale"]

MAX_PAGES = 20


async def _match(db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID | None, invoice_ref: str | None, number: str) -> Invoice | None:
    if invoice_ref:
        link = await integrations_service.find_link(db, installation_id, "invoice", external_id=invoice_ref)
        if link is not None:
            inv = await db.get(Invoice, link.local_id)
            if inv is not None:
                return inv
    if not number:
        return None
    stmt = select(Invoice).where(Invoice.number == number, Invoice.status == "issued")
    if legal_entity_id is not None:
        stmt = stmt.where(Invoice.legal_entity_id == legal_entity_id)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


async def pull_payments(ctx: RequestContext, db: AsyncSession, run: Any) -> integrations_service.SyncResult:
    cfg = await db.get(ProviderConnectorConfig, run.config_id) if run.config_id else None
    config_le = cfg.legal_entity_id if cfg is not None else None
    settings = await get_settings_row(db)
    raw_since = (run.cursor or {}).get("last_run_at")
    if raw_since and (now() - _dt(raw_since)) < timedelta(minutes=settings.pull_payments_every_minutes) and run.trigger == "schedule":
        return integrations_service.SyncResult(skipped=1)  # the tenant's interval is longer than the manifest's
    since = _dt((run.cursor or {}).get("since")) if (run.cursor or {}).get("since") else None
    started = now()
    cursor: str | None = None
    created = skipped = 0
    for _ in range(MAX_PAGES):
        items, cursor = await run.adapter.pull_payments(since, cursor)
        for dto in items:
            if await integrations_service.find_link(db, run.installation_id, "payment", external_id=dto.external_ref) is not None:
                skipped += 1  # our own payment (pushed) or an already confirmed one
                continue
            inv = await _match(db, run.installation_id, config_le, dto.invoice_external_ref, dto.invoice_number)
            le = inv.legal_entity_id if inv is not None else config_le
            if le is None:
                skipped += 1
                continue
            note = None
            if inv is None:
                note = "invoice_not_found"
            elif inv.currency != dto.currency:
                note = "currency_mismatch"
            else:
                bal = await db.get(InvoiceBalance, inv.id)
                open_amount = Decimal(str(bal.open_amount)) if bal is not None else Decimal(str(inv.document_total))
                if Decimal(str(dto.amount)) > open_amount:
                    note = "over_payment"
            mode = dto.mode if dto.mode in PAYMENT_MODES else None
            inserted = (
                await db.execute(
                    pg_insert(PaymentSuggestion)
                    .values(
                        id=uuid.uuid4(), tenant_id=tenant_of(db), installation_id=run.installation_id, legal_entity_id=le,
                        external_ref=dto.external_ref[:200], invoice_id=inv.id if inv else None, invoice_number=(dto.invoice_number or (inv.number if inv else "") or "-")[:60],
                        client_id=inv.client_id if inv else None, amount=dto.amount, currency=dto.currency[:3].upper(), paid_on=dto.paid_on,
                        tds_amount=dto.tds_amount, mode=mode, reference=(dto.reference or None) and dto.reference[:120], match_note=note,
                    )
                    .on_conflict_do_nothing(constraint="uq_payment_suggestions_ref")
                    .returning(PaymentSuggestion.id)
                )
            ).scalar_one_or_none()  # fmt: skip
            if inserted is None:
                skipped += 1
            else:
                created += 1
        if cursor is None:
            break
    stale = await sweep_stale(db, settings.suggestion_stale_days)
    await db.flush()
    if created:
        notify(tenant_of(db), "payment_suggestions_waiting", {"event_id": f"payment_suggestions_waiting:{run.installation_id}:{started.date()}", "count": created, "installation_id": str(run.installation_id)})
    next_cursor = {"since": started.isoformat(), "last_run_at": started.isoformat()} if cursor is None else dict(run.cursor or {})
    return integrations_service.SyncResult(processed=created + skipped, created=created, skipped=skipped, updated=stale, next_cursor=next_cursor)


def _dt(raw: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(raw)


async def sweep_stale(db: AsyncSession, days: int, *, today: date | None = None) -> int:
    cutoff = (today or date.today()) - timedelta(days=days)
    result = await db.execute(update(PaymentSuggestion).where(PaymentSuggestion.status == "pending", PaymentSuggestion.paid_on < cutoff).values(status="stale", updated_at=now()))
    return int(getattr(result, "rowcount", 0) or 0)


async def _names(db: AsyncSession, rows: list[PaymentSuggestion]) -> dict[uuid.UUID, str]:
    ids = {r.client_id for r in rows if r.client_id}
    return dict((await db.execute(select(Client.id, Client.name).where(Client.id.in_(ids)))).all()) if ids else {}


def prefill(s: PaymentSuggestion) -> dict[str, Any] | None:
    """The P2 Record payment form pre-filled from the suggestion (the user may edit it)."""
    if s.client_id is None:
        return None
    data: dict[str, Any] = {
        "legal_entity_id": str(s.legal_entity_id), "client_id": str(s.client_id), "payment_date": s.paid_on.isoformat(),
        "amount": str(s.amount), "currency": s.currency, "mode": s.mode or "neft", "reference": s.reference,
        "allocations": [{"document_kind": "invoice", "document_id": str(s.invoice_id), "amount": str(s.amount)}] if s.invoice_id else "auto",
        "tds": [{"document_kind": "invoice", "document_id": str(s.invoice_id), "kind": "income_tax", "rate_pct": str((Decimal(str(s.tds_amount)) * 100 / (Decimal(str(s.amount)) + Decimal(str(s.tds_amount)))).quantize(Decimal("0.0001"))), "amount": str(s.tds_amount)}] if s.invoice_id and s.tds_amount else [],
    }  # fmt: skip
    return data


async def list_suggestions(ctx: RequestContext, db: AsyncSession, *, status: str | None, legal_entity_id: uuid.UUID | None, page: int = 1, page_size: int = 25) -> Page[PaymentSuggestionOut]:
    stmt = select(PaymentSuggestion)
    if status:
        stmt = stmt.where(PaymentSuggestion.status == status)
    if legal_entity_id:
        stmt = stmt.where(PaymentSuggestion.legal_entity_id == legal_entity_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = list((await db.execute(stmt.order_by(PaymentSuggestion.paid_on.desc(), PaymentSuggestion.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars())
    names = await _names(db, rows)
    items = [PaymentSuggestionOut.model_validate(r).model_copy(update={"client_name": names.get(r.client_id) if r.client_id else None, "prefill": prefill(r)}) for r in rows]
    return Page[PaymentSuggestionOut](items=items, total=total, page=page, page_size=page_size)


async def _suggestion(db: AsyncSession, suggestion_id: uuid.UUID, *, lock: bool = False) -> PaymentSuggestion:
    stmt = select(PaymentSuggestion).where(PaymentSuggestion.id == suggestion_id)
    row = (await db.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Payment suggestion not found")
    return row


def _user(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return p.id if isinstance(p, Principal) and p.type == "user" else None


async def confirm(ctx: RequestContext, db: AsyncSession, suggestion_id: uuid.UUID, data: PaymentIn | None = None) -> PaymentSuggestionOut:
    """★ Record the suggested payment as the confirming User (P2 receivables) and link it (echo rule)."""
    require_human(actor_from_ctx(ctx))
    s = await _suggestion(db, suggestion_id, lock=True)
    if s.status != "pending":
        raise ConflictError("This suggestion was already decided", code="suggestion_decided", details={"status": s.status})
    if data is None:
        form = prefill(s)
        if form is None:
            raise ConflictError("Choose the client and invoice before recording", code="suggestion_unmatched")
        data = PaymentIn.model_validate(form)
    payment = await receivables_service.record_payment(ctx, db, data)
    payment_id = payment.id  # type: ignore[attr-defined]
    await integrations_service.link(db, s.installation_id, "payment", payment_id, s.external_ref)
    s.status, s.payment_id, s.decided_by, s.decided_at = "confirmed", payment_id, _user(ctx), now()
    await db.flush()
    audit.record("ledger.suggestion_confirmed", "payment_suggestion", s.id, session=db, payment_id=str(payment_id))
    emit(db, PaymentSuggestionDecided(tenant_id=s.tenant_id, suggestion_id=s.id, outcome="confirmed", payment_id=payment_id))
    return await _one(db, s)


async def reject(ctx: RequestContext, db: AsyncSession, suggestion_id: uuid.UUID, reason: str) -> PaymentSuggestionOut:
    require_human(actor_from_ctx(ctx))
    s = await _suggestion(db, suggestion_id, lock=True)
    if s.status != "pending":
        raise ConflictError("This suggestion was already decided", code="suggestion_decided", details={"status": s.status})
    s.status, s.reject_reason, s.decided_by, s.decided_at = "rejected", reason.strip()[:500], _user(ctx), now()
    await db.flush()
    audit.record("ledger.suggestion_rejected", "payment_suggestion", s.id, session=db)
    emit(db, PaymentSuggestionDecided(tenant_id=s.tenant_id, suggestion_id=s.id, outcome="rejected", payment_id=None))
    return await _one(db, s)


async def _one(db: AsyncSession, s: PaymentSuggestion) -> PaymentSuggestionOut:
    names = await _names(db, [s])
    return PaymentSuggestionOut.model_validate(s).model_copy(update={"client_name": names.get(s.client_id) if s.client_id else None, "prefill": prefill(s)})
