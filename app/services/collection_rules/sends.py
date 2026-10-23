# ruff: file-ignore[line-too-long, missing-trailing-period, missing-terminal-punctuation]
"""Collections Agent sends (Epic 63 A4, FR-314). Both paths re-plan first (``for_send``), then refuse:

* a hold / takeover -> ``ConflictError collections_agent_paused``;
* ``do_not_remind`` / not open / fully disputed / quiet hours (rule time zone) -> ``ConflictError collections_<reason>``;
* a templated step whose documents are not due for it -> ``ConflictError collections_not_in_plan``.

Recipients are Contacts on record (billing contacts and / or the rule's escalation Contact) -- never an address from an
argument or a Client reply. Only the undisputed balance is referenced; ``invoices_table`` is rendered here from the
balances, never typed by the model. Every message is recorded three times: ``email_messages`` (``origin='agent'``,
related Client + documents), a P2 ``collection_activities`` row (``email``) per document and a ``collection_agent_log``
row; ``CollectionMessageSent`` is emitted. Non-templated text passes ``policy_check`` here again (edited Proposals).
"""

import html
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.core.events import emit
from app.events.collection_rules import CollectionMessageSent
from app.services.collection_rules import facts, holds, log, policy_check, rules

BLOCKING = ("not_open", "do_not_remind", "held", "takeover", "disputed", "quiet_hours")


def _money(v: Decimal) -> str:
    return f"{v:,.2f}"


def invoices_table(rows: list[tuple[str, str, str, str]]) -> str:
    head = "<tr><th>Invoice</th><th>Due date</th><th>Currency</th><th>Amount due</th></tr>"
    body = "".join(f"<tr><td>{html.escape(a)}</td><td>{html.escape(b)}</td><td>{html.escape(c)}</td><td>{html.escape(d)}</td></tr>" for a, b, c, d in rows)
    return f'<table border="1" cellpadding="4" cellspacing="0">{head}{body}</table>'


async def _owner_and_tenant_names(db: AsyncSession, owner_id: uuid.UUID | None) -> tuple[str, str]:
    from app.services import entity_refs

    owner = "Accounts Receivable"
    if owner_id is not None:
        info = (await entity_refs.users(db, [owner_id])).get(owner_id)
        if info is not None:
            owner = info.name
    tenant = await entity_refs.tenant(db, entity_refs.session_tenant(db))
    return owner, str(getattr(tenant, "name", "") or "")


async def _client_name(db: AsyncSession, client_id: uuid.UUID) -> str:
    from app.models.clients import Client

    c = await db.get(Client, client_id)
    return str(c.name) if c is not None else "your organisation"


def _blocked(loaded: facts.Loaded, document_ids: list[uuid.UUID]) -> str | None:
    reasons = {e.document_id: e.reason for e in loaded.plan.excluded}
    for d in document_ids:
        if d not in loaded.docs:
            return "not_open"
        reason = reasons.get(d)
        if reason in BLOCKING:
            return reason
    return None


async def _check(db: AsyncSession, client_id: uuid.UUID, document_ids: list[uuid.UUID]) -> facts.Loaded:
    await holds.assert_not_held(db, client_id, document_ids)
    loaded = await facts.load(db, client_id, for_send=True)
    reason = _blocked(loaded, document_ids)
    if reason in ("held", "takeover"):
        raise ConflictError("The collections agent is paused or taken over for this client or invoice", code="collections_agent_paused")
    if reason is not None:
        raise ConflictError(f"This reminder cannot be sent now ({reason.replace('_', ' ')})", code=f"collections_{reason}"[:50])
    return loaded


async def assert_sendable(
    db: AsyncSession, client_id: uuid.UUID, document_ids: list[uuid.UUID], *, step_id: uuid.UUID | None = None
) -> facts.Loaded:
    """Proposal-time guard (tool previews): the same refusals as a send, except quiet hours (a Proposal may wait)."""
    from app.core.errors import NotFound

    await holds.assert_not_held(db, client_id, document_ids)
    loaded = await facts.load(db, client_id, respect_quiet_hours=False)
    if any(d not in loaded.docs for d in document_ids):
        raise NotFound("Invoice not found")
    reason = _blocked(loaded, document_ids)
    if reason in ("held", "takeover"):
        raise ConflictError("The collections agent is paused or taken over for this client or invoice", code="collections_agent_paused")
    if reason is not None:
        raise ConflictError(f"This reminder cannot be sent ({reason.replace('_', ' ')})", code=f"collections_{reason}"[:50])
    if step_id is not None:
        item = next((i for i in loaded.plan.items if i.step.id == step_id), None)
        if item is None or not set(document_ids) <= {d.doc.id for d in item.docs}:
            raise ConflictError("These invoices are not due for this reminder step", code="collections_not_in_plan")
    return loaded


def _remindable(loaded: facts.Loaded, document_ids: list[uuid.UUID]) -> list[tuple[Any, Decimal]]:
    out = []
    for d in document_ids:
        doc = loaded.docs[d]
        out.append((doc, (doc.open_amount - max(doc.disputed_amount, Decimal(0))).quantize(Decimal("0.01"))))
    return out


async def _deliver(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    loaded: facts.Loaded,
    client_id: uuid.UUID,
    docs: list[tuple[Any, Decimal]],
    audience: str,
    subject: str,
    body_html: str,
    tone: str,
    event: str,
    step_id: uuid.UUID | None,
    owner_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    proposal_id: uuid.UUID | None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    from app.schemas.receivables import ActivityIn
    from app.services import comms_service, receivables_service
    from app.services.comms_service import RecipientSpec

    people = await facts.recipients(db, client_id, audience, loaded.rule)
    if not people:
        raise ValidationFailed("The client has no billing or escalation contact on record", code="no_contact_on_record")
    to = [RecipientSpec(r.role, r.email, "contact", r.contact_id, r.name) for r in people if r.role == "to"]
    cc = [RecipientSpec("cc", r.email, "contact", r.contact_id, r.name) for r in people if r.role == "cc"]
    related: list[tuple[str, uuid.UUID]] = [("client", client_id)] + [(str(doc.kind), doc.id) for doc, _ in docs]
    result = await comms_service.queue_email(
        ctx, db, origin="agent", to=to, cc=cc, subject=subject[:300], body_html=body_html, related=related,
        reply_to_user_id=owner_id, raw=True,
    )  # fmt: skip
    message_id = result.message_ids[0]
    numbers = ", ".join(doc.number or str(doc.id)[:8] for doc, _ in docs)
    activity_id: uuid.UUID | None = None
    for doc, _ in docs:
        act = await receivables_service.log_activity(
            ctx, db,
            ActivityIn(
                client_id=client_id, document_kind=doc.kind, document_id=doc.id, kind="email",
                body=f"Payment reminder sent by Collections Agent ({tone}) for {numbers}"[:5000], owner_id=owner_id,
            ),
        )  # fmt: skip
        activity_id = activity_id or act.id
    await log.write(
        db, client_id=client_id, document_ids=[doc.id for doc, _ in docs], event=event,
        actor={"kind": "agent", "run_id": str(run_id) if run_id else None, "proposal_id": str(proposal_id) if proposal_id else None},
        summary=f"Sent by Collections Agent ({tone}) for {numbers}", rule_step_id=step_id, tone=tone,
        email_message_id=message_id, collection_activity_id=activity_id, agent_run_id=run_id, proposal_id=proposal_id,
    )  # fmt: skip
    emit(db, CollectionMessageSent(
        tenant_id=loaded.rule.tenant_id if loaded.rule is not None else _tenant(db), client_id=client_id,
        document_ids=tuple(doc.id for doc, _ in docs), email_message_id=message_id, tone=tone, via_proposal_id=proposal_id,
    ))  # fmt: skip
    return message_id, activity_id


def _tenant(db: AsyncSession) -> uuid.UUID:
    from app.services import entity_refs

    return entity_refs.session_tenant(db)


def _render(template: str, values: dict[str, str]) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", v)
    return out


async def send_templated(
    ctx: RequestContext,
    db: AsyncSession,
    client_id: uuid.UUID,
    document_ids: list[uuid.UUID],
    step_id: uuid.UUID,
    *,
    owner_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    proposal_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    loaded = await _check(db, client_id, document_ids)
    item = next((i for i in loaded.plan.items if i.step.id == step_id), None)
    planned = {d.doc.id for d in item.docs} if item is not None else set()
    if item is None or not set(document_ids) <= planned:
        raise ConflictError("These invoices are not due for this reminder step", code="collections_not_in_plan")
    step = item.step
    variant = await rules.template_variant(db, step.email_template_id)
    if variant is None:
        raise ValidationFailed("The reminder step has no template", code="step_template_missing")
    docs = _remindable(loaded, document_ids)
    owner_name, tenant_name = await _owner_and_tenant_names(db, owner_id)
    contact = next(iter(await facts.recipients(db, client_id, step.audience, loaded.rule)), None)
    total = sum((r for _, r in docs), Decimal(0))
    due_dates = [d.due_date for d, _ in docs if d.due_date]
    values = {
        "client_name": html.escape(await _client_name(db, client_id)),
        "contact_name": html.escape(contact.name if contact else "Accounts Payable team"),
        "invoices_table": invoices_table([(d.number or "", d.due_date.isoformat() if d.due_date else "", d.currency, _money(r)) for d, r in docs]),
        "total_remindable": _money(total), "currency": html.escape(item.currency),
        "oldest_due_date": min(due_dates).isoformat() if due_dates else "",
        "collections_owner_name": html.escape(owner_name), "tenant_name": html.escape(tenant_name),
    }  # fmt: skip
    subject = _render(variant.subject, {k: html.unescape(v) for k, v in values.items() if k != "invoices_table"})
    body = _render(variant.body_html, values)
    return await _deliver(
        ctx, db, loaded=loaded, client_id=client_id, docs=docs, audience=step.audience, subject=subject, body_html=body,
        tone=step.tone, event="reminder_sent", step_id=step.id, owner_id=owner_id, run_id=run_id, proposal_id=proposal_id,
    )  # fmt: skip


def policy_inputs(loaded: facts.Loaded, document_ids: list[uuid.UUID]) -> tuple[set[Decimal], set[date]]:
    amounts: set[Decimal] = set()
    dates: set[date] = set()
    total = Decimal(0)
    for d in document_ids:
        doc = loaded.docs.get(d)
        if doc is None:
            continue
        remindable = (doc.open_amount - max(doc.disputed_amount, Decimal(0))).quantize(Decimal("0.01"))
        amounts.update({remindable, doc.open_amount.quantize(Decimal("0.01"))})
        total += remindable
        if doc.due_date:
            dates.add(doc.due_date)
    amounts.add(total.quantize(Decimal("0.01")))
    return amounts, dates


async def promised_dates(db: AsyncSession, client_id: uuid.UUID) -> set[date]:
    from app.services import receivables_service

    out = set()
    for a in await receivables_service.recent_activities(db, client_id, 20):
        if a.kind == "promise_to_pay" and a.ptp_date is not None:
            out.add(a.ptp_date)
    return out


def portal_prefixes() -> tuple[str, ...]:
    from app.core.settings import get_settings

    s = get_settings()
    return tuple(str(v) for v in (getattr(s, "public_base_url", None), getattr(s, "portal_base_url", None), getattr(s, "frontend_url", None)) if v)


async def check_message(
    db: AsyncSession, client_id: uuid.UUID, document_ids: list[uuid.UUID], subject: str, body: str, *, loaded: facts.Loaded | None = None
) -> None:
    """``policy_check`` of a non-templated message (proposal time and execution)."""
    loaded = loaded or await facts.load(db, client_id, respect_quiet_hours=False)
    unknown = [d for d in document_ids if d not in loaded.docs]
    if unknown:
        from app.core.errors import NotFound

        raise NotFound("Invoice not found")
    amounts, dates = policy_inputs(loaded, document_ids)
    policy_check.enforce(
        f"{subject}\n{body}", amounts=amounts, due_dates=dates, extra_dates=await promised_dates(db, client_id),
        link_prefixes=portal_prefixes(),
    )  # fmt: skip


def body_html(text: str) -> str:
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)


async def send_message(
    ctx: RequestContext,
    db: AsyncSession,
    client_id: uuid.UUID,
    document_ids: list[uuid.UUID],
    audience: str,
    subject: str,
    body_text: str,
    tone: str,
    *,
    owner_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    proposal_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    loaded = await _check(db, client_id, document_ids)
    await check_message(db, client_id, document_ids, subject, body_text, loaded=loaded)
    docs = _remindable(loaded, document_ids)
    table = invoices_table([(d.number or "", d.due_date.isoformat() if d.due_date else "", d.currency, _money(r)) for d, r in docs])
    step_id = None
    if loaded.rule is not None:
        due = [i for i in loaded.plan.items if {x.doc.id for x in i.docs} & set(document_ids)]
        step_id = due[0].step.id if due else None
    return await _deliver(
        ctx, db, loaded=loaded, client_id=client_id, docs=docs, audience=audience, subject=subject,
        body_html=body_html(body_text) + table, tone=tone, event="message_sent", step_id=step_id, owner_id=owner_id,
        run_id=run_id, proposal_id=proposal_id,
    )  # fmt: skip


async def send_statement(
    ctx: RequestContext,
    db: AsyncSession,
    client_id: uuid.UUID,
    as_of: date,
    *,
    owner_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    proposal_id: uuid.UUID | None = None,
) -> uuid.UUID:
    from app.services import receivables_service

    await holds.assert_not_held(db, client_id)
    loaded = await facts.load(db, client_id, for_send=True)
    settings = await receivables_service.get_client_settings(ctx, db, client_id)
    if settings.do_not_remind:
        raise ConflictError("This client is marked do-not-remind", code="collections_do_not_remind")
    currency = next((d.currency for d in loaded.docs.values()), "INR")
    statement = await receivables_service.build_statement(ctx, db, client_id, currency, as_of - timedelta(days=90), as_of)
    rows = "".join(
        f"<tr><td>{line.date.isoformat()}</td><td>{html.escape(line.description)}</td><td>{_money(line.debit)}</td><td>{_money(line.credit)}</td><td>{_money(line.balance)}</td></tr>"
        for line in statement.lines[-50:]
    )
    owner_name, tenant_name = await _owner_and_tenant_names(db, owner_id)
    body = (
        f"<p>Please find your statement of account as of {as_of.isoformat()}.</p>"
        f"<p>Opening balance: {currency} {_money(statement.opening_balance)}</p>"
        f'<table border="1" cellpadding="4" cellspacing="0"><tr><th>Date</th><th>Description</th><th>Debit</th><th>Credit</th><th>Balance</th></tr>{rows}</table>'
        f"<p>Closing balance: {currency} {_money(statement.closing_balance)}</p>"
        f"<p>Regards,<br>{html.escape(owner_name)}<br>{html.escape(tenant_name)}</p>"
    )
    docs = [(d, d.open_amount) for d in loaded.docs.values() if d.open_amount > 0][:20]
    message_id, _ = await _deliver(
        ctx, db, loaded=loaded, client_id=client_id, docs=docs, audience="billing_contacts",
        subject=f"Statement of account as of {as_of.isoformat()}", body_html=body, tone="courteous", event="message_sent",
        step_id=None, owner_id=owner_id, run_id=run_id, proposal_id=proposal_id,
    )  # fmt: skip
    return message_id


__all__ = [
    "body_html",
    "check_message",
    "invoices_table",
    "policy_inputs",
    "send_message",
    "send_statement",
    "send_templated",
]
