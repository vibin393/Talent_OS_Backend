# ruff: file-ignore[line-too-long]
"""BGV provider follow-ups (Epic 62 A3 / A4, story 62.3, FR-309).

* ``overdue_checks`` -- provider-assigned ``in_progress`` checks past ``due_on`` with the follow-up count, the last date
  and the next due date (every ``FOLLOW_UP_EVERY_WORKING_DAYS`` working days, at most ``MAX_FOLLOW_UPS``).
* ``request_status`` -- providers with a connector: a status poll through the tenant's BGV connector (no email).
* ``send_follow_up`` -- Manual providers: the templated "Agent · BGV status request" to the provider's
  ``contact_email`` **resolved here** (never from arguments); case ref, check type, subject label, external ref and
  dates only -- no documents, links or identity numbers.
* ``draft_message`` -- the non-templated message (only ever executed from an approved Proposal, as the approver).
* ``request_candidate_documents`` -- extra document requests on the case (Proposal only).
Each follow-up writes ``bgv_follow_ups`` and a ``bgv_case_events(kind='provider_follow_up')`` row. Nothing here
adjudicates, waives or records a check result.
"""

import html
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.chasing import BgvFollowUp
from app.schemas.chasing import CaseFollowUpsOut, FollowUpOut, OverdueCheckOut
from app.services import entity_refs
from app.services.chasing import templates

FOLLOW_UP_EVERY_WORKING_DAYS = 2
MAX_FOLLOW_UPS = 5


def now() -> datetime:
    return datetime.now(UTC)


def add_working_days(start: date, days: int) -> date:
    d = start
    added = 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def next_due(due_on: date | None, last_sent: date | None, count: int) -> date | None:
    """Pure cadence: the first follow-up the working day after ``due_on``, then every N working days; None at the limit."""
    if count >= MAX_FOLLOW_UPS or due_on is None:
        return None
    if last_sent is None:
        return add_working_days(due_on, 1)
    return add_working_days(last_sent, FOLLOW_UP_EVERY_WORKING_DAYS)


async def _case(db: AsyncSession, case_id: uuid.UUID) -> Any:
    from app.models.bgv import BgvCase

    case = await db.get(BgvCase, case_id)
    if case is None:
        raise NotFound("BGV case not found")
    return case


async def _check(db: AsyncSession, check_id: uuid.UUID, case_id: uuid.UUID | None) -> Any:
    from app.models.bgv import BgvCheck

    check = await db.get(BgvCheck, check_id)
    if check is None or (case_id is not None and check.case_id != case_id):
        raise NotFound("BGV check not found")
    return check


async def case_owner(db: AsyncSession, case: Any) -> uuid.UUID | None:
    """BGV case owner: the creator -> a check assignee -> a ``bgv:manage`` holder (active users only)."""
    from app.models.bgv import BgvCheck
    from app.services.bgv.common import compliance_recipients

    options: list[uuid.UUID] = []
    if case.created_by:
        options.append(case.created_by)
    options += [
        u
        for u in (
            await db.execute(select(BgvCheck.assignee_user_id).where(BgvCheck.case_id == case.id))
        ).scalars()
        if u is not None
    ]
    if not options:
        options += sorted(await compliance_recipients(db))
    active = await entity_refs.active_user_ids(db, options)
    return next((o for o in options if o in active), None)


async def _follow_ups(db: AsyncSession, check_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[BgvFollowUp]]:
    out: dict[uuid.UUID, list[BgvFollowUp]] = {c: [] for c in check_ids}
    if check_ids:
        for row in (
            await db.execute(
                select(BgvFollowUp).where(BgvFollowUp.check_id.in_(check_ids)).order_by(BgvFollowUp.seq)
            )
        ).scalars():
            out.setdefault(row.check_id, []).append(row)
    return out


async def overdue_checks(db: AsyncSession, case_id: uuid.UUID, today: date) -> list[OverdueCheckOut]:
    from app.models.bgv import BgvCheck, BgvProvider

    checks = list(
        (
            await db.execute(
                select(BgvCheck).where(
                    BgvCheck.case_id == case_id, BgvCheck.status == "in_progress", BgvCheck.assignee_kind == "provider",
                    BgvCheck.due_on.is_not(None), BgvCheck.due_on < today,
                ).order_by(BgvCheck.due_on)
            )
        ).scalars()
    )  # fmt: skip
    fus = await _follow_ups(db, [c.id for c in checks])
    out = []
    for c in checks:
        provider = await db.get(BgvProvider, c.provider_id) if c.provider_id else None
        rows = fus.get(c.id, [])
        last = max((r.sent_at for r in rows), default=None)
        nd = next_due(c.due_on, last.date() if last else None, len(rows))
        out.append(
            OverdueCheckOut(
                check_id=c.id, type=c.type, subject_label=c.subject_label, provider_name=provider.name if provider else None,
                provider_kind=provider.kind if provider else None,
                has_connector=bool(provider is not None and provider.connector_config_id),
                due_on=c.due_on, days_overdue=(today - c.due_on).days, follow_ups_sent=len(rows),
                last_follow_up_on=last.date() if last else None, next_due_on=nd, limit_reached=len(rows) >= MAX_FOLLOW_UPS,
            )
        )  # fmt: skip
    return out


async def cases_with_follow_up_due(db: AsyncSession, today: date) -> list[uuid.UUID]:
    from app.models.bgv import BgvCheck

    case_ids = set(
        (
            await db.execute(
                select(BgvCheck.case_id).where(
                    BgvCheck.status == "in_progress",
                    BgvCheck.assignee_kind == "provider",
                    BgvCheck.due_on < today,
                )
            )
        ).scalars()
    )
    due = []
    for cid in sorted(case_ids):
        rows = await overdue_checks(db, cid, today)
        if any(r.next_due_on is not None and r.next_due_on <= today for r in rows):
            due.append(cid)
    return due


async def _seq(db: AsyncSession, check_id: uuid.UUID) -> int:
    return (
        int(
            (
                await db.execute(
                    select(func.coalesce(func.max(BgvFollowUp.seq), 0)).where(
                        BgvFollowUp.check_id == check_id
                    )
                )
            ).scalar_one()
        )
        + 1
    )


def _event(db: AsyncSession, case: Any, ctx: RequestContext, **detail: Any) -> None:
    from app.services.bgv.common import record_event

    record_event(db, case, "provider_follow_up", ActorRef(ctx.actor_kind or "system", ctx.actor_id), **detail)  # type: ignore[arg-type]


async def _guard(db: AsyncSession, check: Any, today: date) -> int:
    if (
        check.status != "in_progress"
        or check.assignee_kind != "provider"
        or check.due_on is None
        or check.due_on >= today
    ):
        raise ConflictError("The check is not overdue with a provider", code="check_not_overdue")
    rows = (await _follow_ups(db, [check.id]))[check.id]
    if len(rows) >= MAX_FOLLOW_UPS:
        raise ConflictError("The follow-up limit for this check is reached", code="follow_up_limit")
    last = max((r.sent_at for r in rows), default=None)
    nd = next_due(check.due_on, last.date() if last else None, len(rows))
    if nd is None or nd > today:
        raise ConflictError(
            "The next follow-up is not due yet", code="follow_up_not_due", details={"next_due_on": str(nd)}
        )
    return len(rows) + 1


async def request_status(
    ctx: RequestContext,
    db: AsyncSession,
    check_id: uuid.UUID,
    *,
    case_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    today: date,
) -> BgvFollowUp:
    from app.models.bgv import BgvProvider
    from app.models.connectors import ProviderConnectorConfig
    from app.services.connectors import registry

    check = await _check(db, check_id, case_id)
    case = await _case(db, check.case_id)
    provider = await db.get(BgvProvider, check.provider_id) if check.provider_id else None
    if provider is None or not provider.connector_config_id:
        raise ConflictError("This provider has no connector", code="no_connector")
    seq = await _guard(db, check, today)
    row = await db.get(ProviderConnectorConfig, provider.connector_config_id)
    status, provider_status = "failed", None
    try:
        adapter: Any = registry.build("bgv", await registry.load_config(db, row) if row is not None else None)
        if getattr(adapter, "provider_key", "manual") == "manual" or not check.external_ref:
            raise ConflictError("No connector status path for this check", code="no_connector")
        dto = await adapter.get_status(check.external_ref)
        status, provider_status = "status_received", str(dto.status)[:20]
    except ConflictError:
        raise
    except Exception:
        status = "failed"
    fu = BgvFollowUp(
        id=uuid.uuid4(), tenant_id=case.tenant_id, check_id=check.id, case_id=case.id, candidate_id=case.candidate_id,
        provider_id=provider.id, channel="connector", seq=seq, status=status, provider_status=provider_status,
        agent_run_id=run_id, sent_at=now(),
    )  # fmt: skip
    db.add(fu)
    _event(
        db,
        case,
        ctx,
        check_id=check.id,
        channel="connector",
        seq=seq,
        provider_status=provider_status,
        status=status,
    )
    await db.flush()
    return fu


async def _provider_email(db: AsyncSession, check: Any) -> tuple[Any, str]:
    from app.models.bgv import BgvProvider

    provider = await db.get(BgvProvider, check.provider_id) if check.provider_id else None
    if provider is None:
        raise NotFound("BGV provider not found")
    if provider.connector_config_id:
        raise ConflictError("Use the connector for this provider", code="not_manual_provider")
    if not provider.contact_email:
        raise ConflictError("The provider has no contact email on record", code="no_provider_contact")
    return provider, str(provider.contact_email)


async def send_follow_up(
    ctx: RequestContext,
    db: AsyncSession,
    check_id: uuid.UUID,
    *,
    case_id: uuid.UUID | None,
    custom_note: str | None,
    run_id: uuid.UUID | None,
    today: date,
) -> BgvFollowUp:
    from app.services import comms_service

    check = await _check(db, check_id, case_id)
    case = await _case(db, check.case_id)
    provider, address = await _provider_email(db, check)
    seq = await _guard(db, check, today)
    values = {
        "bgv.case_ref": case.human_id, "bgv.check_type": check.type.replace("_", " "), "bgv.subject_label": check.subject_label,
        "bgv.external_ref": check.external_ref or "-", "bgv.started_on": check.started_at.date().isoformat() if check.started_at else "-",
        "bgv.due_on": check.due_on.isoformat() if check.due_on else "-", "bgv.days_overdue": str((today - check.due_on).days) if check.due_on else "-",
    }  # fmt: skip
    rendered = await templates.render(db, templates.BGV_STATUS_REQUEST, values)
    body = rendered.body_html + (f"<p>{html.escape(custom_note)}</p>" if custom_note else "")
    owner = await case_owner(db, case)
    result = await comms_service.queue_email(
        ctx, db, origin="agent", to=[comms_service.RecipientSpec("to", address)], subject=rendered.subject, body_html=body,
        related=[("bgv_case", case.id)], template_variant_id=rendered.variant_id, template_version=rendered.version,
        reply_to_user_id=owner, raw=True,
    )  # fmt: skip
    message_id = result.message_ids[0] if result.message_ids else None
    fu = BgvFollowUp(
        id=uuid.uuid4(), tenant_id=case.tenant_id, check_id=check.id, case_id=case.id, candidate_id=case.candidate_id,
        provider_id=provider.id, channel="email", seq=seq, status="sent" if message_id else "failed",
        email_message_id=message_id, agent_run_id=run_id, sent_at=now(),
    )  # fmt: skip
    db.add(fu)
    _event(db, case, ctx, check_id=check.id, channel="email", seq=seq, email_message_id=message_id)
    await db.flush()
    return fu


async def draft_message(
    ctx: RequestContext,
    db: AsyncSession,
    check_id: uuid.UUID,
    subject: str,
    body: str,
    *,
    proposal_id: uuid.UUID | None,
) -> BgvFollowUp:
    """Send an approved non-templated message to the provider contact on record (executes as the approver)."""
    from app.services import comms_service
    from app.services.agent_runtime.sanitize import outbound

    check = await _check(db, check_id, None)
    case = await _case(db, check.case_id)
    provider, address = await _provider_email(db, check)
    clean = outbound(body, max_len=2000).text
    html_body = "".join(f"<p>{html.escape(line)}</p>" for line in clean.splitlines() if line.strip())
    result = await comms_service.queue_email(
        ctx, db, origin="agent", to=[comms_service.RecipientSpec("to", address)], subject=outbound(subject, max_len=200).text,
        body_html=html_body, related=[("bgv_case", case.id)], reply_to_user_id=await case_owner(db, case), raw=True,
        sender_user_id=getattr(ctx.principal, "id", None) if getattr(ctx.principal, "type", None) == "user" else None,
    )  # fmt: skip
    seq = await _seq(db, check.id)
    fu = BgvFollowUp(
        id=uuid.uuid4(), tenant_id=case.tenant_id, check_id=check.id, case_id=case.id, candidate_id=case.candidate_id,
        provider_id=provider.id, channel="email", seq=seq, status="sent" if result.message_ids else "failed",
        email_message_id=result.message_ids[0] if result.message_ids else None, proposal_id=proposal_id, sent_at=now(),
    )  # fmt: skip
    db.add(fu)
    _event(db, case, ctx, check_id=check.id, channel="email", seq=seq, approved_message=True)
    await db.flush()
    return fu


async def request_candidate_documents(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, keys: list[str], note: str | None
) -> list[uuid.UUID]:
    from app.core.events import emit
    from app.models.bgv import BgvDocumentRequest
    from app.services.bgv.common import DOCUMENTS, doc_label
    from app.services.bgv.events import BgvDocumentsRequested

    case = await _case(db, case_id)
    if case.status in ("clear", "failed", "cancelled"):
        raise ConflictError("The BGV case is closed", code="case_closed")
    unknown = [k for k in keys if k not in DOCUMENTS]
    if unknown:
        raise ValidationFailed("Unknown document key", code="unknown_document", details={"keys": unknown})
    existing = {
        r.key: r
        for r in (
            await db.execute(select(BgvDocumentRequest).where(BgvDocumentRequest.case_id == case.id))
        ).scalars()
    }
    ids: list[uuid.UUID] = []
    for key in dict.fromkeys(keys):
        row = existing.get(key)
        if row is not None and row.status in ("requested", "returned"):
            ids.append(row.id)
            continue
        if row is not None:
            row.status = "requested"
            row.return_note = (note or "")[:300] or None
            ids.append(row.id)
            continue
        label, kind = doc_label(key)
        new = BgvDocumentRequest(
            id=uuid.uuid4(), tenant_id=case.tenant_id, case_id=case.id, candidate_id=case.candidate_id, key=key,
            label=label, kind=kind, required=True, status="requested", return_note=(note or "")[:300] or None,
        )  # fmt: skip
        db.add(new)
        ids.append(new.id)
    _event(db, case, ctx, documents_requested=list(dict.fromkeys(keys)))
    await db.flush()
    emit(db, BgvDocumentsRequested(tenant_id=case.tenant_id, case_id=case.id, candidate_id=case.candidate_id))
    return ids


async def case_summary(db: AsyncSession, case_id: uuid.UUID, today: date) -> dict[str, Any]:
    from app.models.bgv import BgvCheck, BgvDiscrepancy, BgvDocumentRequest

    case = await _case(db, case_id)
    checks = list(
        (
            await db.execute(
                select(BgvCheck).where(BgvCheck.case_id == case.id).order_by(BgvCheck.type, BgvCheck.seq)
            )
        ).scalars()
    )
    docs = list(
        (await db.execute(select(BgvDocumentRequest).where(BgvDocumentRequest.case_id == case.id))).scalars()
    )
    discrepancies = list(
        (await db.execute(select(BgvDiscrepancy).where(BgvDiscrepancy.case_id == case.id))).scalars()
    )
    owner = await case_owner(db, case)
    return {
        "case_id": str(case.id), "case_ref": case.human_id, "status": case.status,
        "owner_user_id": str(owner) if owner else None,
        "checks": [
            {"check_id": str(c.id), "type": c.type, "label": c.subject_label, "status": c.status,
             "overdue": bool(c.overdue or (c.status == "in_progress" and c.due_on is not None and c.due_on < today)),
             "due_on": c.due_on.isoformat() if c.due_on else None}
            for c in checks
        ],
        "document_requests": [{"label": d.label, "status": d.status} for d in docs],
        "discrepancies": [
            {"severity": d.severity, "status": d.status, "response_due": d.response_due_on.isoformat() if getattr(d, "response_due_on", None) else None}
            for d in discrepancies
        ],
    }  # fmt: skip


async def case_follow_ups(db: AsyncSession, case_id: uuid.UUID, today: date) -> CaseFollowUpsOut:
    from app.models.bgv import BgvCheck, BgvProvider

    case = await _case(db, case_id)
    rows = list(
        (
            await db.execute(
                select(BgvFollowUp).where(BgvFollowUp.case_id == case.id).order_by(BgvFollowUp.sent_at.desc())
            )
        ).scalars()
    )
    checks = {
        c.id: c for c in (await db.execute(select(BgvCheck).where(BgvCheck.case_id == case.id))).scalars()
    }
    providers = {p.id: p.name for p in (await db.execute(select(BgvProvider))).scalars()}
    owner = await case_owner(db, case)
    owner_info = await entity_refs.user(db, owner)
    return CaseFollowUpsOut(
        case_id=case.id, max_follow_ups=MAX_FOLLOW_UPS, follow_up_every_working_days=FOLLOW_UP_EVERY_WORKING_DAYS,
        overdue=await overdue_checks(db, case.id, today),
        follow_ups=[
            FollowUpOut(
                id=r.id, check_id=r.check_id, check_type=checks[r.check_id].type if r.check_id in checks else "",
                check_label=checks[r.check_id].subject_label if r.check_id in checks else "",
                provider_name=providers.get(r.provider_id) if r.provider_id else None, channel=r.channel,
                seq=r.seq, status=r.status, provider_status=r.provider_status,
                days_overdue=(r.sent_at.date() - checks[r.check_id].due_on).days if r.check_id in checks and checks[r.check_id].due_on else None,
                sent_at=r.sent_at,
            )
            for r in rows
        ],
        limit_task_owner_name=owner_info.name if owner_info else None,
    )  # fmt: skip


__all__ = [
    "FOLLOW_UP_EVERY_WORKING_DAYS",
    "MAX_FOLLOW_UPS",
    "add_working_days",
    "case_follow_ups",
    "case_owner",
    "case_summary",
    "cases_with_follow_up_due",
    "draft_message",
    "next_due",
    "overdue_checks",
    "request_candidate_documents",
    "request_status",
    "send_follow_up",
]
