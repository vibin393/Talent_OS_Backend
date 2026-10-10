"""Pre-joining (Epic 19 A6, P2-6.3, FR-105): touchpoints, joining-confirmation links, unmet-gate alerts.

* ``run_daily`` (beat ``placements.daily``): for each ``pre_joining|joining_confirmed`` placement whose
  ``planned_start_date - today`` is one of the tenant's ``touchpoint_offsets`` and has no row yet, insert the
  touchpoint, issue a ``joining_confirmation`` Secure Link (TTL until the start date + 1 day, one use) and queue the
  tenant email (sent by the Celery task after commit). It also raises unmet-gate alerts at
  T-``gate_alert_offset_days`` (once per placement per day) and re-evaluates the dropout rules.
* ``link_view`` never mutates; ``link_respond`` consumes the link: ``confirmed`` -> ``joining_confirmed`` (actor
  ``secure_link``), ``question`` -> the recruiter is notified.
"""

import dataclasses
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.core.events import emit
from app.core.mailer import EmailMessage
from app.models.placements import Placement, PlacementTouchpoint, StartGate
from app.schemas.placements_portal import JoiningLinkInfo, JoiningResponseIn
from app.services.placements import joining
from app.services.placements.common import (
    DEFAULT_TOUCHPOINT_TEMPLATE,
    PRE_START,
    local_today,
    now,
    settings_row,
)
from app.services.placements.events import JoiningQuestionRaised, StartGatesUnmet

PURPOSE = "joining_confirmation"
_ENV = SandboxedEnvironment(autoescape=True)


@dataclasses.dataclass
class DailyResult:
    touchpoints: int = 0
    alerts: int = 0
    raised: int = 0
    emails: list[EmailMessage] = dataclasses.field(default_factory=list)


def render_touchpoint(template: str | None, **values: Any) -> str:
    try:
        return _ENV.from_string(template or DEFAULT_TOUCHPOINT_TEMPLATE).render(**values)
    except Exception:
        return _ENV.from_string(DEFAULT_TOUCHPOINT_TEMPLATE).render(**values)


async def _context(db: AsyncSession, p: Placement) -> dict[str, Any]:
    from app.services import entity_refs, identity_bridge
    from app.services.placements.common import names

    tenant = await identity_bridge.get_tenant(db, p.tenant_id)
    who = await entity_refs.addressee(db, "candidate", p.candidate_id)
    _, clients = await names(db, client_ids={p.client_id})
    return {
        "first_name": (who.full_name or "").split(" ")[0] if who and who.full_name else "",
        "email": who.address if who else None,
        "client_name": clients.get(p.client_id) or "",
        "role_title": p.role_title or "",
        "start_date": p.planned_start_date.strftime("%d %b %Y"),
        "tenant_name": tenant.name if tenant else "",
    }


async def issue_touchpoint(
    ctx: RequestContext,
    db: AsyncSession,
    p: Placement,
    offset: int,
    today: date,  # noqa: ARG001
) -> EmailMessage | None:
    inserted = await db.execute(
        pg_insert(PlacementTouchpoint)
        .values(
            id=uuid.uuid4(),
            tenant_id=p.tenant_id,
            placement_id=p.id,
            offset_days=offset,
            due_on=p.planned_start_date - timedelta(days=offset),
        )
        .on_conflict_do_nothing(constraint="uq_placement_touchpoints_offset")
        .returning(PlacementTouchpoint.id)
    )
    tp_id = inserted.scalar_one_or_none()
    if tp_id is None:
        return None  # already issued (idempotent)
    expires = datetime.combine(p.planned_start_date + timedelta(days=1), time(23, 59), tzinfo=UTC)
    link, token = await secure_links.issue(
        db,
        tenant_id=p.tenant_id,
        purpose=PURPOSE,
        subject_type="placement_touchpoint",
        subject_id=tp_id,
        ttl=max(expires - now(), timedelta(hours=1)),
        max_uses=1,
        metadata={"placement_id": str(p.id)},
    )
    tp = (await db.execute(select(PlacementTouchpoint).where(PlacementTouchpoint.id == tp_id))).scalar_one()
    tp.secure_link_id = link.id
    tp.sent_at = now()
    await db.flush()
    values = await _context(db, p)
    if not values.get("email"):
        return None
    settings = await settings_row(db, p.tenant_id)
    body = render_touchpoint(settings.touchpoint_email_template, link=secure_links.link_url(token), **values)
    return EmailMessage(
        to=str(values["email"]),
        subject=f"Your start date: {values['start_date']}",
        text=body,
        tenant_id=p.tenant_id,
        kind="placement_joining_confirmation",
    )


async def run_daily(ctx: RequestContext, db: AsyncSession) -> DailyResult:
    out = DailyResult()
    assert ctx.tenant_id is not None
    settings = await settings_row(db, ctx.tenant_id)
    offsets = sorted(set(settings.touchpoint_offsets or []))
    horizon = max([*offsets, settings.gate_alert_offset_days, 7]) + 1
    rows = list(
        (
            await db.execute(
                select(Placement).where(
                    Placement.status.in_(PRE_START),
                    Placement.planned_start_date >= date.today() - timedelta(days=1),
                    Placement.planned_start_date <= date.today() + timedelta(days=horizon + 1),
                )
            )
        ).scalars()
    )
    for p in rows:
        today = await local_today(db, p)
        days = (p.planned_start_date - today).days
        if days in offsets:
            msg = await issue_touchpoint(ctx, db, p, days, today)
            if msg is not None:
                out.emails.append(msg)
                out.touchpoints += 1
        if days == settings.gate_alert_offset_days and p.gate_alert_sent_on != today:
            unmet = (
                await db.execute(
                    select(StartGate.id).where(
                        StartGate.placement_id == p.id,
                        StartGate.required.is_(True),
                        StartGate.status.not_in(("met", "waived", "not_required")),
                    )
                )
            ).all()
            if unmet:
                p.gate_alert_sent_on = today
                await db.flush()
                recipients = tuple(
                    {u for u in (p.account_manager_user_id, *await _hr_users(db)) if u is not None}
                )
                emit(
                    db,
                    StartGatesUnmet(
                        tenant_id=p.tenant_id,
                        placement_id=p.id,
                        planned_start_date=p.planned_start_date,
                        unmet=len(unmet),
                        recipient_ids=recipients,
                    ),
                )
                out.alerts += 1
        before = p.dropout_risk
        await joining.evaluate_dropout(ctx, db, p)
        if p.dropout_risk and not before:
            out.raised += 1
    return out


async def _hr_users(db: AsyncSession) -> list[uuid.UUID]:
    from app.services import identity_bridge

    try:
        return [u["id"] for u in await identity_bridge.users_with_permission(db, "worker_bank:approve")]
    except Exception:
        return []


# ================================================================ secure link page
async def _resolve(token: str) -> tuple[secure_links.ResolvedLink, uuid.UUID]:
    link = await secure_links.resolve(token, (PURPOSE,))
    raw = link.meta.get("placement_id")
    if not raw:
        raise NotFound("Link not found", code="link_invalid")
    return link, uuid.UUID(str(raw))


async def link_view(token: str) -> tuple[secure_links.ResolvedLink, JoiningLinkInfo, str | None]:
    from app.services import entity_refs, identity_bridge
    from app.services.placements.common import names

    link, placement_id = await _resolve(token)
    async with tenant_session(link.tenant_id) as db:
        p = (await db.execute(select(Placement).where(Placement.id == placement_id))).scalar_one_or_none()
        if p is None:
            raise NotFound("Link not found", code="link_invalid")
        who = await entity_refs.addressee(db, "candidate", p.candidate_id)
        _, clients = await names(db, client_ids={p.client_id})
        tenant = await identity_bridge.get_tenant(db, p.tenant_id)
        info = JoiningLinkInfo(
            client_name=clients.get(p.client_id),
            role_title=p.role_title,
            planned_start_date=p.planned_start_date,
            candidate_first_name=(who.full_name or "").split(" ")[0] if who and who.full_name else None,
            responded=p.status != "pre_joining",
        )
    return link, info, tenant.name if tenant else None


async def link_respond(token: str, data: JoiningResponseIn) -> str:
    link, placement_id = await _resolve(token)
    ctx = RequestContext(tenant_id=link.tenant_id)
    async with tenant_session(link.tenant_id) as db:
        await secure_links.consume(db, link.id)
        p = (
            await db.execute(select(Placement).where(Placement.id == placement_id).with_for_update())
        ).scalar_one_or_none()
        if p is None:
            raise NotFound("Link not found", code="link_invalid")
        tp = (
            await db.execute(
                select(PlacementTouchpoint).where(PlacementTouchpoint.id == link.subject_id).with_for_update()
            )
        ).scalar_one_or_none()
        if tp is not None:
            tp.responded_at = now()
            tp.response = data.response
            tp.response_note = data.note
            await db.flush()
        if data.response == "confirmed":
            await joining.confirm_joining(ctx, db, p, ActorRef("secure_link", link.id), "secure_link")
        else:
            emit(
                db,
                JoiningQuestionRaised(
                    tenant_id=p.tenant_id, placement_id=p.id, recruiter_user_id=p.recruiter_user_id
                ),
            )
    return "confirmed" if data.response == "confirmed" else "question_sent"
