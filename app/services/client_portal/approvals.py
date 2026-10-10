"""Client Portal approvals hub (Epic 18 A4, FR-102): every approval request whose **current step** resolves to
the signed-in Client User, or to a colleague they are an active delegate for, across subject kinds.

Reads run in the Client session (org RLS: the engine grants each Client step's request to the client org), then
the step is matched in Python (``client_user`` -> the user / a delegator; ``client_any`` -> same client and the
step's portal permission). Restricted users see only subjects of ``visible_placement_ids`` / ``visible_demand_ids``.
Summaries come from the subject domains' ``portal.py`` ``client_`` functions (downward calls; an absent epic just
falls back to the engine's ``describe``). Decisions go through ``approvals.decide`` in an internal session with
``ActorRef(kind='client_user', id=me, on_behalf_of=delegator|None)``; the subject callback runs in the same
transaction (AD-24). Timesheets are decided on Epic 25's endpoints (409 ``use_subject_endpoint``).
"""

import dataclasses
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import ActorRef
from app.core.client_scope import visible_demand_ids, visible_placement_ids
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.approvals import ApprovalRequest
from app.models.external_auth import ClientUser
from app.schemas.approvals import DecideIn
from app.schemas.client_hub_portal import (
    ApprovalDetailOut,
    ApprovalItemOut,
    ApprovalKindOut,
    ApprovalsHubOut,
    SummaryField,
)
from app.services import client_delegations
from app.services.client_portal_base import client_of

CLIENT_KINDS = ("client_user", "client_any")
SUBJECT_ENDPOINT_KINDS = frozenset({"timesheet"})
LABELS = {
    "timesheet": "Timesheets",
    "expense_claim": "Expenses",
    "demand": "Demands",
    "placement_extension": "Extensions",
    "extension": "Extensions",
    "offboarding_confirmation": "Offboarding",
}
LINKS = {"timesheet": "/client/timesheets/{id}", "expense_claim": "/client/expenses/{id}"}
NEWEST_PER_KIND = 50


@dataclasses.dataclass(frozen=True, slots=True)
class Mine:
    request: ApprovalRequest
    acting_for: uuid.UUID | None


def _step(request: ApprovalRequest) -> dict[str, Any]:
    return next((s for s in request.chain_snapshot if int(s["position"]) == request.current_position), {})


async def _matches(ctx: RequestContext, db: AsyncSession, requests: list[ApprovalRequest]) -> list[Mine]:
    _, client_id, me = client_of(ctx)
    delegators = await client_delegations.acting_for(db, me)
    perms: dict[uuid.UUID, set[str]] = {me: set(getattr(ctx.principal, "portal_permissions", None) or ())}
    if delegators:
        for u in (await db.execute(select(ClientUser).where(ClientUser.id.in_(delegators)))).scalars():
            perms[u.id] = set(u.portal_permissions or [])
    places = await visible_placement_ids(ctx, db)
    demands = await visible_demand_ids(ctx, db)
    out: list[Mine] = []
    for r in requests:
        if r.status != "pending" or r.current_approver_kind not in CLIENT_KINDS:
            continue
        facts = r.facts or {}
        if (
            places is not None
            and facts.get("placement_id")
            and uuid.UUID(str(facts["placement_id"])) not in places
        ):
            continue
        if demands is not None and r.subject_type == "demand" and r.subject_id not in demands:
            continue
        if r.current_approver_kind == "client_user":
            target = r.current_approver_client_user_id
            if target == me:
                out.append(Mine(r, None))
            elif target in delegators:
                out.append(Mine(r, target))
            continue
        step = _step(r)
        org = step.get("org") or [None, None]
        if str(org[1]) != str(client_id):
            continue
        wanted = (step.get("approver_ref") or {}).get("portal_permission")
        if wanted is None or wanted in perms[me]:
            out.append(Mine(r, None))
            continue
        other = next((d for d in delegators if wanted in perms.get(d, set())), None)
        if other is not None:
            out.append(Mine(r, other))
    return out


async def _pending(
    ctx: RequestContext, db: AsyncSession, request_ids: list[uuid.UUID] | None = None
) -> list[Mine]:
    stmt = select(ApprovalRequest).where(
        ApprovalRequest.status == "pending", ApprovalRequest.current_approver_kind.in_(CLIENT_KINDS)
    )
    if request_ids is not None:
        stmt = stmt.where(ApprovalRequest.id.in_(request_ids))
    rows = list((await db.execute(stmt.order_by(ApprovalRequest.submitted_at.desc()).limit(1000))).scalars())
    return await _matches(ctx, db, rows)


async def _names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await db.execute(select(ClientUser.id, ClientUser.display_name).where(ClientUser.id.in_(ids)))
    return {r[0]: r[1] for r in rows.all()}


async def _summaries(
    ctx: RequestContext, db: AsyncSession, kind: str, requests: list[ApprovalRequest]
) -> dict[uuid.UUID, tuple[str, str | None, list[tuple[str, str | None]]]]:
    """``request_id -> (title, subtitle, fields)`` from the subject domain (guarded downward calls)."""
    ids = [r.id for r in requests]
    by_subject = {r.subject_id: r.id for r in requests}
    out: dict[uuid.UUID, tuple[str, str | None, list[tuple[str, str | None]]]] = {}
    try:
        if kind == "timesheet":
            from app.services.time import portal as time_portal

            for s in await time_portal.client_timesheet_summaries(ctx, db, ids):
                fields = [
                    ("Worker", s.worker_name),
                    ("Period", f"{s.period_start} – {s.period_end}"),
                    ("Total hours", str(s.total_hours)),
                ]
                fields += [(f"Hours ({k})", str(v)) for k, v in s.totals.items()]
                if s.flags.ot_above_threshold:
                    fields.append(("Flag", "Overtime"))
                if s.flags.holiday_entries:
                    fields.append(("Flag", "Holiday entries"))
                if s.flags.above_daily_cap:
                    fields.append(("Flag", "Above daily cap"))
                rid = s.approval_request_id or by_subject.get(s.id)
                if rid:
                    out[rid] = (
                        f"{s.worker_name} · {s.period_start} – {s.period_end}",
                        f"{s.total_hours} h",
                        fields,
                    )
        elif kind == "expense_claim":
            from app.services.expenses import portal as expenses_portal

            for c in await expenses_portal.client_claim_summaries(ctx, db, ids):
                data = c.model_dump()
                rid = data.get("approval_request_id") or by_subject.get(data.get("id"))  # type: ignore[arg-type]
                title = f"{data.get('worker_name') or 'Expense claim'} · {data.get('human_id') or ''}".strip(
                    " ·"
                )
                fields = [
                    (k.replace("_", " ").capitalize(), str(v))
                    for k, v in data.items()
                    if isinstance(v, str | int) and k not in ("id",)
                ]
                if rid:
                    out[rid] = (title, None, fields[:10])
        elif kind == "demand":
            from app.services.demands import portal as demands_portal

            for sid, s in (await demands_portal.client_demand_summary(ctx, db, list(by_subject))).items():
                rid = by_subject.get(sid)
                if rid:
                    out[rid] = (s["title"], s["subtitle"], s["fields"])
    except Exception:
        logger.exception("Client approvals hub: {} summaries unavailable", kind)
    missing = [r for r in requests if r.id not in out]
    if missing:
        from app.services.approvals.registry import subject

        definition = subject(kind)
        described: dict[uuid.UUID, Any] = {}
        if definition is not None and definition.describe is not None:
            try:
                described = await definition.describe(ctx, db, [r.subject_id for r in missing])
            except Exception:
                logger.exception("Client approvals hub: describe failed for {}", kind)
        for r in missing:
            d = described.get(r.subject_id)
            title = (d.title if d else None) or LABELS.get(kind, kind.replace("_", " ").capitalize())
            out[r.id] = (title, d.label if d else None, [("Item", title)])
    return out


def _item(
    m: Mine, summary: tuple[str, str | None, list[tuple[str, str | None]]], names: dict[uuid.UUID, str]
) -> ApprovalItemOut:
    r = m.request
    link = LINKS.get(r.subject_type)
    return ApprovalItemOut(
        request_id=r.id,
        kind=r.subject_type,
        subject_id=r.subject_id,
        title=summary[0],
        subtitle=summary[1],
        submitted_at=r.submitted_at,
        acting_for_id=m.acting_for,
        acting_for_name=names.get(m.acting_for) if m.acting_for else None,
        decide_via="subject_endpoint" if r.subject_type in SUBJECT_ENDPOINT_KINDS else "generic",
        link=link.format(id=r.subject_id) if link else None,
    )


async def _with_summaries(
    ctx: RequestContext, mine: list[Mine]
) -> list[tuple[Mine, tuple[str, str | None, list[tuple[str, str | None]]]]]:
    tenant_id, _, _ = client_of(ctx)
    if not mine:
        return []
    out: list[tuple[Mine, Any]] = []
    async with tenant_session(tenant_id) as idb:
        by_kind: dict[str, list[Mine]] = {}
        for m in mine:
            by_kind.setdefault(m.request.subject_type, []).append(m)
        for kind, items in by_kind.items():
            sums = await _summaries(ctx, idb, kind, [m.request for m in items])
            out += [(m, sums[m.request.id]) for m in items]
    return out


async def hub(ctx: RequestContext, db: AsyncSession) -> ApprovalsHubOut:
    mine = await _pending(ctx, db)
    names = await _names(db, {m.acting_for for m in mine if m.acting_for})
    grouped: dict[str, list[ApprovalItemOut]] = {}
    for m, s in await _with_summaries(ctx, mine):
        grouped.setdefault(m.request.subject_type, []).append(_item(m, s, names))
    kinds = [
        ApprovalKindOut(
            kind=k,
            label=LABELS.get(k, k.replace("_", " ").capitalize()),
            count=len(v),
            items=v[:NEWEST_PER_KIND],
        )
        for k, v in sorted(grouped.items())
    ]
    return ApprovalsHubOut(total=sum(k.count for k in kinds), kinds=kinds)


async def pending_count(ctx: RequestContext, db: AsyncSession) -> int:
    return len(await _pending(ctx, db))


async def _one(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> Mine:
    found = await _pending(ctx, db, [request_id])
    if not found:
        raise NotFound("Approval request not found")
    return found[0]


async def detail(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> ApprovalDetailOut:
    m = await _one(ctx, db, request_id)
    names = await _names(db, {m.acting_for} if m.acting_for else set())
    (pair,) = await _with_summaries(ctx, [m])
    return ApprovalDetailOut(
        item=_item(m, pair[1], names),
        fields=[SummaryField(label=k, value=v) for k, v in pair[1][2]],
        status=m.request.status,
    )


async def decide(
    ctx: RequestContext,
    db: AsyncSession,
    request_id: uuid.UUID,
    decision: str,
    comment: str | None,
    subject_payload: dict[str, Any] | None = None,
) -> ApprovalDetailOut:
    from app.services import approvals as approvals_service

    tenant_id, _, me = client_of(ctx)
    m = await _one(ctx, db, request_id)
    if m.request.subject_type in SUBJECT_ENDPOINT_KINDS:
        raise ConflictError(
            "Decide this item on its own page",
            code="use_subject_endpoint",
            details={"kind": m.request.subject_type},
        )
    text = (comment or "").strip() or None
    if decision == "reject" and text is None:
        raise ValidationFailed("A comment is required", code="comment_required")
    names = await _names(db, {m.acting_for} if m.acting_for else set())
    (pair,) = await _with_summaries(ctx, [m])
    actor = ActorRef("client_user", me, on_behalf_of=m.acting_for)
    async with tenant_session(tenant_id) as idb:
        audit.set_actor(idb, "client_user", me)
        await approvals_service.decide(
            ctx,
            idb,
            request_id,
            DecideIn(action=decision, comment=text, subject_payload=subject_payload),
            actor=actor,  # type: ignore[arg-type]
        )
        status = (
            await idb.execute(select(ApprovalRequest.status).where(ApprovalRequest.id == request_id))
        ).scalar_one()
    item = _item(m, pair[1], names)
    return ApprovalDetailOut(
        item=item, fields=[SummaryField(label=k, value=v) for k, v in pair[1][2]], status=status
    )


async def summary_for_link(
    ctx: RequestContext, db: AsyncSession, request: ApprovalRequest
) -> tuple[str, str | None, list[tuple[str, str | None]]]:
    """Secure Link page projection (internal session ``db``)."""
    return (await _summaries(ctx, db, request.subject_type, [request]))[request.id]
