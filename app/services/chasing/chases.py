# ruff: file-ignore[line-too-long]
"""Compliance chasing (Epic 62 A3 / A4, stories 62.1-62.2, FR-308): plan view, templated reminders, escalations.

* ``plan_view`` -- the deterministic plan of a Worker (``plan.due_chases`` over ``facts.gather``) with the channels
  that may be used; structured only (labels are tenant configuration).
* ``send_reminder`` -- re-validates every item against today's plan (``item_not_due``), then sends ONE templated
  reminder per channel: portal notification ``compliance_chase_reminder``, email "Agent · Document reminder" (or the
  credential variant) through ``comms_service`` (``origin='agent'``), SMS / WhatsApp through a registered channel
  template with key ``agent_document_reminder`` (else ``suppressed:no_template``). A channel that is not usable is
  recorded as suppressed and never replaced by another channel. Writes one ``compliance_chases`` row per item and a
  candidate (and placement) timeline entry.
* ``escalate`` -- a Task to the onboarding coordinator, ``ChaseEscalated``, ``compliance_chase_escalation``.
"""

import dataclasses
import html
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.events.chasing import ChaseEscalated
from app.models.chasing import ComplianceChase
from app.services import entity_refs
from app.services.chasing import facts as facts_mod
from app.services.chasing import templates
from app.services.chasing.plan import DEFAULT_CONFIG, ChasePlan, DueItem, due_chases

REASON_LABELS = {
    "portal_not_opened": "portal not opened",
    "deadline_close": "deadline within 2 days",
    "repeated_no_response": "no response to repeated reminders",
}
CADENCE = {
    "offsets": list(DEFAULT_CONFIG.offsets),
    "post_deadline_every_days": DEFAULT_CONFIG.post_deadline_every_days,
    "max_post_deadline_chases": DEFAULT_CONFIG.max_post_deadline_chases,
    "escalate_within_days": DEFAULT_CONFIG.escalate_within_days,
    "portal_window_days": DEFAULT_CONFIG.portal_window_days,
    "person_quiet_hours": DEFAULT_CONFIG.person_quiet_hours,
    "item_quiet_hours": DEFAULT_CONFIG.item_quiet_hours,
}


def now() -> datetime:
    return datetime.now(UTC)


async def local_today(db: AsyncSession) -> date:
    tenant = await entity_refs.tenant(db, entity_refs.session_tenant(db))
    return datetime.now(tenant.tz).date()


@dataclasses.dataclass(frozen=True, slots=True)
class PlanResult:
    plan: ChasePlan
    bundle: facts_mod.FactsBundle
    today: date


async def compute(db: AsyncSession, candidate_id: uuid.UUID, today: date | None = None) -> PlanResult:
    day = today or await local_today(db)
    bundle = await facts_mod.gather(db, candidate_id, day, now())
    return PlanResult(due_chases(bundle.facts, day), bundle, day)


async def plan_view(db: AsyncSession, candidate_id: uuid.UUID, today: date | None = None) -> dict[str, Any]:
    r = await compute(db, candidate_id, today)
    return {
        "candidate_id": str(candidate_id),
        "today": r.today.isoformat(),
        "items": [
            {
                "item_kind": i.item_kind,
                "item_ref": str(i.item_ref),
                "label": i.label,
                "due_basis": i.due_basis,
                "due_on": i.due_on.isoformat(),
                "days_left": i.days_left,
                "offset_days": i.offset_days,
                "placement_id": str(i.placement_id) if i.placement_id else None,
            }
            for i in r.plan.items
        ],
        "allowed_channels": list(r.bundle.allowed_channels),
        "unavailable_channels": [{"channel": c, "reason": why} for c, why in r.bundle.unavailable],
        "last_contact": r.bundle.facts.last_contact.isoformat() if r.bundle.facts.last_contact else None,
        "portal_last_seen": r.bundle.facts.portal_last_seen.isoformat()
        if r.bundle.facts.portal_last_seen
        else None,
        "escalate": r.plan.escalate,
        "escalation_reasons": list(r.plan.reasons),
        "coordinator_id": str(r.bundle.coordinator_id) if r.bundle.coordinator_id else None,
        "placement_ids": [str(p) for p in r.bundle.placements],
        "skipped": [{"item_ref": str(ref), "reason": why} for ref, why in r.plan.skipped],
    }


# ================================================================ reminders
def _subject_line(items: list[DueItem]) -> tuple[str, str]:
    """(template name, deadline text)."""
    first = min(items, key=lambda i: i.due_on)
    name = templates.DOCUMENT_REMINDER if first.due_basis == "start_date" else templates.CREDENTIAL_REMINDER
    return name, first.due_on.strftime("%d %b %Y")


async def _portal(
    db: AsyncSession, candidate_id: uuid.UUID, items: list[DueItem], deadline: str, key: str
) -> str | None:
    from app.services import notifications_service as ns

    basis = min(items, key=lambda i: i.due_on).due_basis
    title = (
        f"Documents needed before {deadline}"
        if basis == "start_date"
        else f"Your {items[0].label} expires on {deadline}"
    )
    try:
        await ns.deliver(
            db, "compliance_chase_reminder", [ns.Recipient("candidate", candidate_id)],
            ns.Content(title=title[:300], body=", ".join(i.label for i in items)[:500], link_path="/portal/work/onboarding"),
            event_id=f"chase:{key}", payload={"basis": basis},
        )  # fmt: skip
    except Exception:
        logger.exception("compliance_chase_reminder not delivered to candidate {}", candidate_id)
        return "delivery_failed"
    return None


async def _email(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    items: list[DueItem],
    deadline: str,
    custom_note: str | None,
    owner_name: str,
) -> tuple[uuid.UUID | None, str | None]:
    from app.core.settings import get_settings
    from app.services import comms_service

    name, _ = _subject_line(items)
    rendered = await templates.render(
        db,
        name,
        {
            "reminder.deadline": deadline,
            "reminder.items_html": templates.items_html([i.label for i in items]),
        },
    )
    body = rendered.body_html
    if custom_note:
        body += f"<p>{html.escape(custom_note)}</p>"
    body += (
        f"<p><em>{html.escape(get_settings().agent_disclosure_line.format(owner_name=owner_name))}</em></p>"
    )
    try:
        recipient = await comms_service.recipient_for(db, "candidate", candidate_id)
    except (NotFound, ValidationFailed):
        return None, "no_address"
    related: list[tuple[str, uuid.UUID]] = [("candidate", candidate_id)]
    related += [("placement", i.placement_id) for i in items if i.placement_id]
    result = await comms_service.queue_email(
        ctx, db, origin="agent", to=[recipient], subject=rendered.subject, body_html=body,
        refs={"candidate_id": candidate_id}, related=list(dict.fromkeys(related)),
        template_variant_id=rendered.variant_id, template_version=rendered.version,
    )  # fmt: skip
    if not result.message_ids or result.warnings.suppressed_addresses:
        return None, "suppressed"
    if result.warnings.bounced_addresses:
        return result.message_ids[0], None
    return result.message_ids[0], None


async def _channel(
    ctx: RequestContext, db: AsyncSession, channel: str, candidate_id: uuid.UUID
) -> tuple[uuid.UUID | None, str | None]:
    from app.models.comms import ChannelTemplate
    from app.schemas.messaging import RecipientRef
    from app.services.messaging import sending

    template = (
        await db.execute(
            select(ChannelTemplate).where(
                ChannelTemplate.channel == channel,
                ChannelTemplate.key == "agent_document_reminder",
                ChannelTemplate.status == "registered",
            )
        )
    ).scalar_one_or_none()
    if template is None:
        return None, "no_template"
    try:
        out = await sending.send(
            ctx, db, channel, template.id, [RecipientRef(entity_type="candidate", entity_id=candidate_id)],
            {"candidate_id": candidate_id}, origin="agent",
        )  # fmt: skip
    except Exception as exc:  # channel disabled, template not sendable ...
        return None, getattr(exc, "code", "send_failed")
    first = out.results[0] if out.results else None
    if first is None or first.status != "queued":
        return None, (first.reason if first is not None else "send_failed") or "refused"
    return first.message_id, None


async def send_reminder(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    requested: list[tuple[str, uuid.UUID, int]],
    channels: list[str],
    custom_note: str | None,
    *,
    run_id: uuid.UUID | None,
    owner_user_id: uuid.UUID | None,
) -> dict[str, Any]:
    r = await compute(db, candidate_id)
    due = {(i.item_kind, i.item_ref): i for i in r.plan.items}
    items: list[DueItem] = []
    for kind, ref, offset in requested:
        item = due.get((kind, ref))  # type: ignore[arg-type]
        if item is None or item.offset_days != offset:
            raise ConflictError(
                "This item is not due for a reminder today",
                code="item_not_due",
                details={"item_ref": str(ref)},
            )
        if item not in items:
            items.append(item)
    if not items:
        raise ConflictError("Nothing is due for a reminder today", code="item_not_due")
    allowed = set(r.bundle.allowed_channels)
    unavailable = dict(r.bundle.unavailable)
    owner = await entity_refs.user(db, owner_user_id)
    owner_name = owner.name if owner is not None else "your coordinator"
    _, deadline = _subject_line(items)
    key = f"{candidate_id}:{r.today.isoformat()}:{run_id or uuid.uuid4()}"
    sent: list[str] = []
    suppressed: list[dict[str, str]] = []
    refs: list[dict[str, str]] = []
    for channel in dict.fromkeys(channels):
        if channel not in allowed:
            suppressed.append({"channel": channel, "reason": unavailable.get(channel, "not_allowed")})
            continue
        if channel == "portal":
            failure = await _portal(db, candidate_id, items, deadline, key)
            message_id = None
        elif channel == "email":
            message_id, failure = await _email(
                ctx, db, candidate_id, items, deadline, custom_note, owner_name
            )
        else:
            message_id, failure = await _channel(ctx, db, channel, candidate_id)
        if failure:
            suppressed.append({"channel": channel, "reason": failure})
            continue
        sent.append(channel)
        if message_id is not None:
            refs.append({"channel": channel, "message_id": str(message_id)})
    stamp = now()
    chase_ids: list[uuid.UUID] = []
    tenant_id = entity_refs.session_tenant(db)
    for item in items:
        row = ComplianceChase(
            id=uuid.uuid4(), tenant_id=tenant_id, candidate_id=candidate_id, placement_id=item.placement_id,
            item_kind=item.item_kind, item_ref=item.item_ref, item_label=item.label[:200], due_basis=item.due_basis,
            due_on=item.due_on, offset_days=item.offset_days, channels=sent, suppressed=suppressed, escalated=False,
            agent_run_id=run_id, message_refs=refs, sent_at=stamp,
        )  # fmt: skip
        db.add(row)
        chase_ids.append(row.id)
    await db.flush()
    await _timeline(ctx, db, candidate_id, items, sent, suppressed)
    return {"chase_ids": chase_ids, "sent": sent, "suppressed": suppressed}


async def _timeline(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    items: list[DueItem],
    sent: list[str],
    suppressed: list[dict[str, str]],
) -> None:
    from app.services import activity_service

    for item in items:
        days = abs(item.days_left)
        when = (
            f"{days} days to {'start' if item.due_basis == 'start_date' else 'expiry'}"
            if item.days_left >= 0
            else f"{days} days past {'start' if item.due_basis == 'start_date' else 'expiry'}"
        )
        body = f"Document reminder sent by Compliance Agent: {item.label} ({when})"
        if sent:
            body += f" via {', '.join(sent)}"
        if suppressed:
            body += "; not sent: " + ", ".join(f"{s['channel']} ({s['reason']})" for s in suppressed)
        data = {"source": "compliance_agent", "item_kind": item.item_kind, "offset_days": item.offset_days}
        try:
            await activity_service.add_system_entry(ctx, db, "candidate", candidate_id, body[:2000], data)
            if item.placement_id is not None:
                await activity_service.add_system_entry(
                    ctx, db, "placement", item.placement_id, body[:2000], data
                )
        except Exception:
            logger.exception("Chase timeline entry failed for candidate {}", candidate_id)


# ================================================================ escalations
async def escalate(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    placement_id: uuid.UUID | None,
    reason: str,
    *,
    run_id: uuid.UUID | None,  # ruff: ignore[unused-function-argument] - kept for the audit trail of the caller
) -> uuid.UUID:
    from app.models.tasks import Task
    from app.schemas.tasks import TaskCreate
    from app.services import notifications_service as ns
    from app.services import tasks_service

    r = await compute(db, candidate_id)
    chases = list(
        (
            await db.execute(select(ComplianceChase).where(ComplianceChase.candidate_id == candidate_id))
        ).scalars()
    )
    repeated = any(sum(1 for c in chases if c.item_ref == ref) >= 2 for ref in {c.item_ref for c in chases})
    if reason not in r.plan.reasons and not (reason == "repeated_no_response" and repeated):
        raise ConflictError(
            "No escalation is due for this Worker", code="escalation_not_due", details={"reason": reason}
        )
    if placement_id is not None and placement_id not in r.bundle.placements:
        raise NotFound("Placement not found")
    placement_id = placement_id or next(iter(r.bundle.placements), None)
    owner = r.bundle.coordinator_id
    if owner is None:
        raise ConflictError("No onboarding coordinator to escalate to", code="no_coordinator")
    missing = [
        i
        for i in r.bundle.facts.items
        if i.item_kind != "checklist_item" or i.status in ("missing", "expiring", "expired")
    ]
    n = len({i.item_ref for i in missing}) or len(r.plan.items)
    who = await entity_refs.addressee(db, "candidate", candidate_id)
    worker = (who.full_name if who is not None and who.full_name else None) or "Worker"
    title = (
        f"Call {worker} — portal not opened, {n} items missing"
        if reason == "portal_not_opened"
        else f"Chase {worker}: {n} compliance items missing ({REASON_LABELS.get(reason, reason)})"
    )[:200]
    entity_type, entity_id = ("placement", placement_id) if placement_id else ("candidate", candidate_id)
    start = datetime.combine(r.today, time(0, 0), tzinfo=UTC)
    existing = (
        await db.execute(
            select(Task.id)
            .where(
                Task.entity_type == entity_type,
                Task.entity_id == entity_id,
                Task.title == title,
                Task.created_at >= start - timedelta(hours=14),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    data = TaskCreate.model_construct(
        title=title, notes=f"Escalated by the Compliance Agent ({REASON_LABELS.get(reason, reason)}). Pre-start risk: /compliance/pre-start-risk?candidate={candidate_id}",
        owner_id=owner, entity_type=entity_type, entity_id=entity_id, due_at=now() + timedelta(hours=8), due_all_day=False,  # type: ignore[arg-type]
        priority="high" if "deadline_close" in r.plan.reasons else "normal", reminder_at=None,
    )  # fmt: skip
    task = await tasks_service.create_system_task(ctx, db, data, source="agent")
    await db.execute(
        update(ComplianceChase)
        .where(
            ComplianceChase.candidate_id == candidate_id,
            ComplianceChase.sent_at >= start - timedelta(hours=14),
        )
        .values(escalated=True, task_id=task.id)
        .execution_options(synchronize_session=False)
    )
    tenant_id = entity_refs.session_tenant(db)
    emit(
        db,
        ChaseEscalated(
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            placement_id=placement_id,
            reason=reason,
            task_id=task.id,
            item_count=n,
        ),
    )
    recipients = {owner}
    p = r.bundle.placements.get(placement_id) if placement_id else None
    if p is not None and getattr(p, "account_manager_user_id", None):
        recipients.add(p.account_manager_user_id)  # type: ignore[attr-defined]
    try:
        await ns.deliver(
            db, "compliance_chase_escalation", list(recipients),
            ns.Content(title=f"{worker}: {n} compliance items still missing ({REASON_LABELS.get(reason, reason)})"[:300],
                       entity_type="candidate", entity_id=candidate_id, link_path=f"/compliance/pre-start-risk?candidate={candidate_id}"),
            event_id=f"chase_escalation:{task.id}",
        )  # fmt: skip
    except Exception:
        logger.exception("compliance_chase_escalation not delivered (task {})", task.id)
    await db.flush()
    return task.id


# ================================================================ reads
async def list_chases(
    db: AsyncSession,
    *,
    candidate_id: uuid.UUID | None = None,
    placement_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[ComplianceChase]:
    stmt = select(ComplianceChase).order_by(ComplianceChase.sent_at.desc()).limit(limit)
    if candidate_id is not None:
        stmt = stmt.where(ComplianceChase.candidate_id == candidate_id)
    if placement_id is not None:
        stmt = stmt.where(ComplianceChase.placement_id == placement_id)
    return list((await db.execute(stmt)).scalars())


async def due_candidates(db: AsyncSession, today: date) -> list[uuid.UUID]:
    """Workers with at least one item due today (coarse SQL prefilter, then the plan decides -- they always agree)."""
    from app.models.compliance import Credential, PlacementComplianceItem
    from app.models.placements import Placement

    ids = set(
        (
            await db.execute(
                select(Placement.candidate_id)
                .join(PlacementComplianceItem, PlacementComplianceItem.placement_id == Placement.id)
                .where(
                    Placement.status.in_(facts_mod.OPEN_PLACEMENT),
                    PlacementComplianceItem.mandatory.is_(True),
                    PlacementComplianceItem.status.in_(("missing", "expiring", "expired")),
                )
            )
        ).scalars()
    )
    horizon = max(DEFAULT_CONFIG.offsets)
    back = DEFAULT_CONFIG.post_deadline_every_days * DEFAULT_CONFIG.max_post_deadline_chases
    ids |= set(
        (
            await db.execute(
                select(Credential.candidate_id).where(
                    Credential.superseded_at.is_(None), Credential.erased_at.is_(None),
                    Credential.expires_on.between(today - timedelta(days=back), today + timedelta(days=horizon)),
                )
            )
        ).scalars()
    )  # fmt: skip
    out = []
    for cid in sorted(ids):
        r = await compute(db, cid, today)
        if r.plan.items:
            out.append(cid)
    return out


__all__ = [
    "CADENCE",
    "REASON_LABELS",
    "compute",
    "due_candidates",
    "escalate",
    "list_chases",
    "local_today",
    "plan_view",
    "send_reminder",
]
