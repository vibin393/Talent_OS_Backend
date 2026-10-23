# ruff: file-ignore[line-too-long, invalid-formatter-suppression-comment]
"""Collections (FR-156): human-driven (no AI).

Activities with promises-to-pay, per-client settings (owner, do-not-remind), the overdue queue (disputed
amounts excluded), reminder emails and the daily beats:

* ``ptp_check`` -- an open PTP whose ``ptp_date + grace`` passed with less than ``ptp_amount`` allocated to the
  targeted documents since the PTP becomes ``broken``: a P1 Task for the owner (``source='collections'``) and
  ``PromiseToPayBroken``.
* ``overdue_scan`` -- emits ``InvoiceOverdue`` (days-overdue fact) for open overdue invoices.

Reminders are refused (skipped, reported) for disputed documents and do-not-remind clients, whoever sends them
(user or workflow). Client Users never see collection activities.
"""

import html
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.events.billing import InvoiceOverdue
from app.events.receivables import PromiseToPayBroken
from app.models.receivables import (
    Allocation,
    AllocationReversal,
    ClientCollectionSettings,
    CollectionActivity,
)
from app.schemas.receivables import (
    ActivityIn,
    ActivityOut,
    CollectionSettingsIn,
    CollectionSettingsOut,
    QueueRow,
    ReminderIn,
    ReminderOut,
)
from app.services.receivables import settlement
from app.services.receivables.common import ZERO, documents, invalid, q2, tenant_of, today, user_id
from app.services.receivables.settings import get_settings_row


# ---------------------------------------------------------------- settings
async def _settings_map(db: AsyncSession, client_ids: set[uuid.UUID] | None = None) -> dict[uuid.UUID, ClientCollectionSettings]:
    stmt = select(ClientCollectionSettings)
    if client_ids is not None:
        stmt = stmt.where(ClientCollectionSettings.client_id.in_(client_ids))
    return {s.client_id: s for s in (await db.execute(stmt)).scalars()}


async def get_client_settings(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID) -> CollectionSettingsOut:  # ruff: ignore[unused-function-argument]
    s = (await _settings_map(db, {client_id})).get(client_id)
    return CollectionSettingsOut(client_id=client_id, do_not_remind=bool(s and s.do_not_remind), owner_id=s.owner_id if s else None)


async def put_client_settings(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: CollectionSettingsIn) -> CollectionSettingsOut:
    values = dict(data.model_dump(exclude_unset=True).items())
    stmt = pg_insert(ClientCollectionSettings.__table__).values(tenant_id=tenant_of(db), client_id=client_id, **values)
    if values:
        stmt = stmt.on_conflict_do_update(index_elements=["tenant_id", "client_id"], set_=values)
    else:
        stmt = stmt.on_conflict_do_nothing()
    await db.execute(stmt)
    audit.record("collections.settings_updated", "client", client_id, session=db, **{k: str(v) for k, v in values.items()})
    return await get_client_settings(ctx, db, client_id)


# ---------------------------------------------------------------- activities
async def log_activity(ctx: RequestContext, db: AsyncSession, data: ActivityIn) -> ActivityOut:
    if data.kind == "promise_to_pay" and (data.ptp_amount is None or data.ptp_date is None):
        raise invalid("ptp_fields_required", "A promise to pay needs an amount and a date")
    if (data.document_kind is None) != (data.document_id is None):
        raise invalid("document_ref", "Give both document kind and id, or neither")
    owner = data.owner_id or user_id(ctx)
    row = CollectionActivity(
        id=uuid.uuid4(), tenant_id=tenant_of(db), client_id=data.client_id, document_kind=data.document_kind, document_id=data.document_id,
        kind=data.kind, body=data.body, ptp_amount=data.ptp_amount, ptp_currency=data.ptp_currency or ("INR" if data.ptp_amount else None),
        ptp_date=data.ptp_date, ptp_status="open" if data.kind == "promise_to_pay" else None, owner_id=owner, created_by=user_id(ctx),
    )  # fmt: skip
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return ActivityOut.model_validate(row)


async def list_activities(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID | None = None, ptp_status: str | None = None) -> list[ActivityOut]:  # ruff: ignore[unused-function-argument]
    stmt = select(CollectionActivity)
    if client_id:
        stmt = stmt.where(CollectionActivity.client_id == client_id)
    if ptp_status:
        stmt = stmt.where(CollectionActivity.ptp_status == ptp_status)
    return [ActivityOut.model_validate(a) for a in (await db.execute(stmt.order_by(CollectionActivity.occurred_at.desc()).limit(500))).scalars()]


# ---------------------------------------------------------------- queue
async def queue(ctx: RequestContext, db: AsyncSession, *, legal_entity_id: uuid.UUID | None = None, client_id: uuid.UUID | None = None, min_days: int = 1) -> list[QueueRow]:  # ruff: ignore[unused-function-argument]
    on = await today(db)
    docs = [d for d in await documents(db, legal_entity_id=legal_entity_id, client_id=client_id) if d.status == "issued" and d.due_date and (on - d.due_date).days >= min_days]
    bals = await settlement.balances(db, docs)
    settings = await _settings_map(db, {d.client_id for d in docs})
    from app.services.receivables.aging import _client_names

    names = await _client_names(db, {d.client_id for d in docs})
    ptps = {
        r[0]: r[1]
        for r in (
            await db.execute(
                select(CollectionActivity.document_id, func.min(CollectionActivity.ptp_date)).where(CollectionActivity.ptp_status == "open", CollectionActivity.document_id.is_not(None)).group_by(CollectionActivity.document_id)
            )
        ).all()
    }
    out = []
    for b in bals.values():
        collectible = q2(b.open - b.disputed)
        if b.open <= 0:
            continue
        d = b.doc
        s = settings.get(d.client_id)
        out.append(
            QueueRow(
                client_id=d.client_id, client_name=names.get(d.client_id), document_kind=d.kind, document_id=d.id, number=d.number, due_date=d.due_date,
                days_overdue=(on - d.due_date).days if d.due_date else 0, currency=d.currency, open_amount=b.open, disputed_amount=b.disputed,
                collectible=max(collectible, ZERO), do_not_remind=bool(s and s.do_not_remind), open_ptp_date=ptps.get(d.id),
            )
        )
    return sorted(out, key=lambda r: (-r.days_overdue, -r.open_amount))


# ---------------------------------------------------------------- reminders
async def send_reminders(ctx: RequestContext, db: AsyncSession, data: ReminderIn, *, actor_kind: str = "user") -> ReminderOut:
    from app.services import commercials_service, comms_service
    from app.services.comms_service import RecipientSpec

    docs = {d.id: d for d in await documents(db, ids=list(data.document_ids))}
    bals = await settlement.balances(db, list(docs.values()))
    settings = await _settings_map(db, {d.client_id for d in docs.values()})
    subject_tpl, body_tpl = data.subject, data.body
    if data.template_id is not None:
        from app.models.comms import EmailTemplate, EmailTemplateVariant

        tpl = (await db.execute(select(EmailTemplate).where(EmailTemplate.id == data.template_id))).scalar_one_or_none()
        if tpl is None:
            raise NotFound("Email template not found")
        variant = (await db.execute(select(EmailTemplateVariant).where(EmailTemplateVariant.template_id == tpl.id).limit(1))).scalar_one_or_none()
        if variant is not None:
            subject_tpl = subject_tpl or variant.subject
            body_tpl = body_tpl or variant.body_html
    sent: list[uuid.UUID] = []
    skipped: list[dict[str, Any]] = []
    for doc_id in data.document_ids:
        d = docs.get(doc_id)
        if d is None:
            skipped.append({"document_id": str(doc_id), "reason": "not_found"})
            continue
        b = bals[doc_id]
        s = settings.get(d.client_id)
        if s is not None and s.do_not_remind:
            skipped.append({"document_id": str(doc_id), "reason": "do_not_remind"})
            continue
        if b.disputed > 0:
            skipped.append({"document_id": str(doc_id), "reason": "disputed"})
            continue
        if b.open <= 0 or d.status != "issued":
            skipped.append({"document_id": str(doc_id), "reason": "not_open"})
            continue
        try:
            profile = await commercials_service.get_billing_profile(ctx, db, d.client_id)
            to = [RecipientSpec("to", a) for a in profile.billing_contacts.to]
            cc = [RecipientSpec("cc", a) for a in profile.billing_contacts.cc]
        except Exception:
            to, cc = [], []
        if not to:
            skipped.append({"document_id": str(doc_id), "reason": "no_billing_contact"})
            continue
        facts = {"number": d.number or "", "open_amount": f"{b.open:,.2f}", "currency": d.currency, "due_date": d.due_date.isoformat() if d.due_date else ""}
        subject = (subject_tpl or "Payment reminder: {number}").format_map(_Safe(facts))
        body = (body_tpl or "<p>Invoice {number} has {currency} {open_amount} outstanding (due {due_date}). Please arrange payment.</p>").format_map(_Safe({k: html.escape(v) for k, v in facts.items()}))
        await comms_service.queue_email(ctx, db, origin="system", to=to, cc=cc, subject=subject, body_html=body, related=[("client", d.client_id)], raw=True)
        db.add(
            CollectionActivity(
                id=uuid.uuid4(), tenant_id=tenant_of(db), client_id=d.client_id, document_kind=d.kind, document_id=d.id, kind="email",
                body=f"Reminder sent ({actor_kind}): {subject}"[:2000], owner_id=user_id(ctx), created_by=user_id(ctx),
            )
        )
        sent.append(doc_id)
    await db.flush()
    return ReminderOut(sent=sent, skipped=skipped)


class _Safe(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


# ---------------------------------------------------------------- beats
async def _allocated_since(db: AsyncSession, document_id: uuid.UUID, since: date) -> Decimal:
    doc = func.coalesce(Allocation.invoice_id, Allocation.debit_note_id)
    a = (await db.execute(select(func.coalesce(func.sum(Allocation.amount), 0)).where(doc == document_id, Allocation.allocation_date >= since))).scalar()
    r = (
        await db.execute(
            select(func.coalesce(func.sum(AllocationReversal.amount), 0)).join(Allocation, Allocation.id == AllocationReversal.allocation_id).where(doc == document_id, Allocation.allocation_date >= since)
        )
    ).scalar()
    return Decimal(a or 0) + Decimal(r or 0)


async def ptp_check(ctx: RequestContext, db: AsyncSession, on: date | None = None) -> int:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    on = on or await today(db)
    grace = (await get_settings_row(db)).ptp_grace_days
    rows = list((await db.execute(select(CollectionActivity).where(CollectionActivity.ptp_status == "open", CollectionActivity.ptp_date < on - timedelta(days=grace)).with_for_update(skip_locked=True))).scalars())
    broken = 0
    for p in rows:
        since = p.occurred_at.date()
        paid = await _allocated_since(db, p.document_id, since) if p.document_id else ZERO
        if paid >= (p.ptp_amount or ZERO):
            p.ptp_status = "kept"
            continue
        p.ptp_status = "broken"
        if p.owner_id is not None:
            try:
                task = await tasks_service.create_system_task(
                    ctx, db,
                    TaskCreate(title="Promise to pay broken - follow up", owner_id=p.owner_id, due_at=datetime.combine(on, time(12, 0), tzinfo=UTC), entity_type="client", entity_id=p.client_id),
                    source="collections",
                )  # fmt: skip
                p.task_id = task.id
            except Exception:
                from loguru import logger

                logger.exception("collections: PTP task for {} failed", p.id)
        emit(db, PromiseToPayBroken(tenant_id=p.tenant_id, promise_id=p.id, invoice_id=p.document_id or p.id, client_id=p.client_id))
        broken += 1
    await db.flush()
    return broken


async def overdue_scan(ctx: RequestContext, db: AsyncSession, on: date | None = None) -> int:  # ruff: ignore[unused-function-argument]
    on = on or await today(db)
    docs = [d for d in await documents(db) if d.kind == "invoice" and d.status == "issued" and d.due_date and d.due_date < on]
    bals = await settlement.balances(db, docs, with_disputes=False)
    count = 0
    for b in bals.values():
        if b.open > 0:
            emit(db, InvoiceOverdue(tenant_id=tenant_of(db), invoice_id=b.doc.id, client_id=b.doc.client_id, days_overdue=(on - b.doc.due_date).days))  # type: ignore[operator]
            count += 1
    return count
