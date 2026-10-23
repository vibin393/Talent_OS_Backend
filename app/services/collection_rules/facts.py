# ruff: file-ignore[line-too-long]
"""Loader of the collection plan facts and of the Client's message recipients (Epic 63 A4).

Recipients come from records only: the billing profile's Contacts (``billing_profile_contacts``, roles ``to`` / ``cc``;
P2 already stores Contact ids, so build-plan Known gap 1 does not arise) and the rule's escalation Contact. Archived or
address-less Contacts are skipped (``suppressed:no_contact_record``). Never an address from text.
"""

import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.clients import Contact
from app.models.collection_rules import CollectionAgentLog, CollectionRule, CollectionRuleStep
from app.schemas.collection_rules import CollectionPlanOut, Excluded, PlanDocument, PlanItem
from app.services.collection_rules import holds, log, plan, rules

RECENT_CONTACT_DAYS = 3
SENT_EVENTS = ("reminder_sent", "message_sent")


@dataclasses.dataclass(frozen=True, slots=True)
class Loaded:
    client_id: uuid.UUID
    today: date
    rule: CollectionRule | None
    steps: list[CollectionRuleStep]
    docs: dict[uuid.UUID, plan.DocFacts]
    plan: plan.CollectionPlan
    held: bool


@dataclasses.dataclass(frozen=True, slots=True)
class ContactRecipient:
    contact_id: uuid.UUID
    name: str
    email: str
    role: str  # to | cc


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("Asia/Kolkata")


async def _sent_since(db: AsyncSession, client_id: uuid.UUID, since: datetime) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(CollectionAgentLog)
                .where(
                    CollectionAgentLog.client_id == client_id, CollectionAgentLog.event.in_(SENT_EVENTS),
                    CollectionAgentLog.at >= since,
                )
            )
        ).scalar_one()
    )  # fmt: skip


async def load(
    db: AsyncSession,
    client_id: uuid.UUID,
    *,
    now: datetime | None = None,
    for_send: bool = False,
    respect_quiet_hours: bool = True,
) -> Loaded:
    """Facts + plan. ``for_send`` counts only messages actually sent as done (a pending Proposal's own step stays due
    when it is approved)."""
    from app.services import receivables_service

    rule, steps = await rules.effective_rule(db, client_id)
    tz = _zone(rule.timezone if rule is not None else None)
    now = now or datetime.now(UTC)
    local = now.astimezone(tz)
    today = local.date()
    docs = await receivables_service.collectible_documents(db, client_id)
    settings = await receivables_service.get_client_settings(RequestContext(tenant_id=None), db, client_id)
    client_hold, doc_holds = await holds.holds_map(db, client_id)
    done = await log.done_steps(db, client_id)
    if for_send:
        sent = (
            await db.execute(
                select(CollectionAgentLog.document_ids, CollectionAgentLog.rule_step_id).where(
                    CollectionAgentLog.client_id == client_id, CollectionAgentLog.event.in_(SENT_EVENTS),
                    CollectionAgentLog.rule_step_id.is_not(None),
                )
            )
        ).all()  # fmt: skip
        done = {(d, s) for ds, s in sent for d in ds or []}
    recent = await receivables_service.email_activity_since(db, client_id, now - timedelta(days=RECENT_CONTACT_DAYS))
    facts_docs = {
        d.id: plan.DocFacts(
            kind=d.kind, id=d.id, number=d.number, due_date=d.due_date, currency=d.currency, open_amount=d.open_amount,
            disputed_amount=d.disputed_amount, status=d.status, written_off=d.written_off,
        )
        for d in docs
    }  # fmt: skip
    step_facts = tuple(
        plan.StepFacts(
            id=s.id, position=s.position, offset_days=s.offset_days, tone=s.tone, audience=s.audience,
            email_template_id=s.email_template_id, allow_auto=s.allow_auto, create_task=s.create_task, active=s.active,
        )
        for s in steps
    )  # fmt: skip
    result = plan.collection_plan(
        plan.PlanFacts(
            client_id=client_id, today=today, docs=tuple(facts_docs.values()), steps=step_facts,
            now_local=local if respect_quiet_hours else None, rule_active=bool(rule is not None and rule.active),
            do_not_remind=settings.do_not_remind, client_hold=client_hold, doc_holds=doc_holds, done=frozenset(done),
            recently_contacted=frozenset() if for_send else frozenset(recent),
            sent_last_7_days=await _sent_since(db, client_id, now - timedelta(days=7)),
            max_per_week=rule.max_reminders_per_week if rule is not None else 2,
            quiet_start=rule.quiet_start if rule is not None else plan.time(18, 0),
            quiet_end=rule.quiet_end if rule is not None else plan.time(9, 0),
        )
    )  # fmt: skip
    return Loaded(client_id, today, rule, steps, facts_docs, result, client_hold is not None)


def to_out(loaded: Loaded) -> CollectionPlanOut:
    items = [
        PlanItem(
            step_id=i.step.id, position=i.step.position, offset_days=i.step.offset_days, tone=i.step.tone,
            audience=i.step.audience, email_template_id=i.step.email_template_id, send_class=i.send_class,  # type: ignore[arg-type]
            create_task=i.step.create_task, currency=i.currency, total_remindable=i.total_remindable,
            partially_disputed=i.partially_disputed,
            documents=[
                PlanDocument(
                    document_kind=d.doc.kind, document_id=d.doc.id, number=d.doc.number, due_date=d.doc.due_date,
                    days_overdue=d.days_overdue, currency=d.doc.currency, open_amount=d.doc.open_amount,
                    disputed_amount=d.doc.disputed_amount, remindable_amount=d.remindable,
                )
                for d in i.docs
            ],
        )
        for i in loaded.plan.items
    ]  # fmt: skip
    return CollectionPlanOut(
        client_id=loaded.client_id, today=loaded.today, rule_id=loaded.rule.id if loaded.rule else None,
        rule_name=loaded.rule.name if loaded.rule else None, held=loaded.held, items=items,
        excluded=[Excluded(document_id=e.document_id, number=e.number, reason=e.reason) for e in loaded.plan.excluded],
    )  # fmt: skip


async def billing_contacts(db: AsyncSession, client_id: uuid.UUID) -> tuple[list[ContactRecipient], list[str]]:
    from app.models.commercials import BillingProfileContact

    rows = (
        await db.execute(
            select(BillingProfileContact.contact_id, BillingProfileContact.role, Contact.name, Contact.email, Contact.archived_at, Contact.client_id)
            .join(Contact, Contact.id == BillingProfileContact.contact_id)
            .where(BillingProfileContact.client_id == client_id)
        )
    ).all()
    out: list[ContactRecipient] = []
    suppressed: list[str] = []
    for cid, role, name, email, archived, owner_client in rows:
        if owner_client != client_id or archived is not None or not email:
            suppressed.append(f"suppressed:no_contact_record:{cid}")
            continue
        out.append(ContactRecipient(cid, name, str(email), "cc" if role == "cc" else "to"))
    return out, suppressed


async def escalation_contact(db: AsyncSession, client_id: uuid.UUID, rule: CollectionRule | None) -> ContactRecipient | None:
    if rule is None or rule.escalation_contact_id is None:
        return None
    c = await db.get(Contact, rule.escalation_contact_id)
    if c is None or c.client_id != client_id or c.archived_at is not None or not c.email:
        return None
    return ContactRecipient(c.id, c.name, str(c.email), "to")


async def recipients(
    db: AsyncSession, client_id: uuid.UUID, audience: str, rule: CollectionRule | None
) -> list[ContactRecipient]:
    out: list[ContactRecipient] = []
    if audience in ("billing_contacts", "both"):
        out += (await billing_contacts(db, client_id))[0]
    if audience in ("escalation_contact", "both"):
        esc = await escalation_contact(db, client_id, rule)
        if esc is not None and all(r.contact_id != esc.contact_id for r in out):
            out.append(dataclasses.replace(esc, role="to"))
    if out and all(r.role == "cc" for r in out):
        out[0] = dataclasses.replace(out[0], role="to")
    return out


async def collections_owner(db: AsyncSession, client_id: uuid.UUID | None) -> uuid.UUID | None:
    from app.models.clients import Client
    from app.models.receivables import ClientCollectionSettings

    if client_id is None:
        return None
    owner = (
        await db.execute(select(ClientCollectionSettings.owner_id).where(ClientCollectionSettings.client_id == client_id))
    ).scalar_one_or_none()
    if owner is not None:
        return uuid.UUID(str(owner))
    client = await db.get(Client, client_id)
    return client.owner_id if client is not None else None


__all__ = ["ContactRecipient", "Loaded", "billing_contacts", "collections_owner", "escalation_contact", "load", "recipients", "to_out"]
