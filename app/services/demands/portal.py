"""Client Portal functions of the demands domain (Epic 18, FR-100). Append-only ``client_`` functions.

Reads run in the Client session (org RLS ``client``) plus ``visible_demand_ids`` for restricted Client Users.
Drafts / pending-approval demands are visible only when raised through the portal by this client. A demand
request is written in an internal tenant session on behalf of the Client User: a Draft with
``requested_by_client_user_id``, owned by the client's Account Manager (``clients.owner_id``), who is notified.
"""

import uuid
from collections import Counter
from typing import Any

from loguru import logger
from sqlalchemy import column, func, select, table, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.client_portal import ClientDemandRequested
from app.models.clients import Client
from app.models.demands import Demand, DemandDocument
from app.models.pipeline import Submission
from app.schemas.demands_portal import ClientDemandRequestIn, ClientDemandRequestOut, ClientDemandSummary
from app.services.client_portal_base import client_of, require_demand_scope
from app.services.demands.common import demand_settings
from app.services.demands.constants import REQ_SEQUENCE_KEY

_demands = table("demands", column("id"), column("requested_by_client_user_id"))
HIDDEN_UNLESS_REQUESTED = ("draft", "pending_approval")
JD_KINDS = ("pdf", "docx")


async def _requested_by(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID | None]:
    if not ids:
        return {}
    rows = await db.execute(
        select(_demands.c.id, _demands.c.requested_by_client_user_id).where(_demands.c.id.in_(ids))
    )
    return {r[0]: r[1] for r in rows.all()}


async def _sent_counts(db: AsyncSession, ids: list[uuid.UUID]) -> Counter[uuid.UUID]:
    if not ids:
        return Counter()
    rows = await db.execute(
        select(Submission.demand_id, func.count())
        .where(Submission.demand_id.in_(ids), Submission.status == "sent")
        .group_by(Submission.demand_id)
    )
    return Counter({r[0]: int(r[1]) for r in rows.all()})


def _visible(d: Demand, requested: uuid.UUID | None) -> bool:
    return d.status not in HIDDEN_UNLESS_REQUESTED or requested is not None


async def _summaries(ctx: RequestContext, db: AsyncSession, rows: list[Demand]) -> list[ClientDemandSummary]:
    _, _, me = client_of(ctx)
    ids = [d.id for d in rows]
    requested = await _requested_by(db, ids)
    sent = await _sent_counts(db, ids)
    return [
        ClientDemandSummary(
            id=d.id,
            req_id=d.req_id,
            title=d.title,
            positions=d.positions,
            status=d.status,
            location=d.location,
            start_date=d.start_date,
            sent_submissions=sent.get(d.id, 0),
            requested_by_me=requested.get(d.id) == me,
            requested_via_portal=requested.get(d.id) is not None,
            created_at=d.created_at,
        )
        for d in rows
        if _visible(d, requested.get(d.id))
    ]


async def client_list_demands(
    ctx: RequestContext, db: AsyncSession, status: str | None = None
) -> list[ClientDemandSummary]:
    from app.core.client_scope import visible_demand_ids

    _, client_id, _ = client_of(ctx)
    stmt = select(Demand).where(Demand.client_id == client_id, Demand.archived_at.is_(None))
    if status:
        stmt = stmt.where(Demand.status == status)
    scope = await visible_demand_ids(ctx, db)
    if scope is not None:
        stmt = stmt.where(Demand.id.in_(scope or [uuid.uuid4()]))
    rows = list((await db.execute(stmt.order_by(Demand.created_at.desc()).limit(500))).scalars())
    return await _summaries(ctx, db, rows)


async def client_get_demand(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID
) -> ClientDemandSummary:
    _, client_id, _ = client_of(ctx)
    d = (
        await db.execute(select(Demand).where(Demand.id == demand_id, Demand.client_id == client_id))
    ).scalar_one_or_none()
    if d is None:
        raise NotFound("Demand not found")
    await require_demand_scope(ctx, db, demand_id)
    found = await _summaries(ctx, db, [d])
    if not found:
        raise NotFound("Demand not found")
    return found[0]


async def client_demand_summary(
    ctx: RequestContext, db: AsyncSession, demand_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Approvals hub projection (``demand`` subject): title / req id / positions / location."""
    rows = (await db.execute(select(Demand).where(Demand.id.in_(demand_ids)))).scalars()
    return {
        d.id: {
            "title": f"{d.req_id} · {d.title}",
            "subtitle": f"{d.positions} position(s){' · ' + d.location if d.location else ''}",
            "fields": [
                ("Demand", d.title),
                ("Reference", d.req_id),
                ("Positions", str(d.positions)),
                ("Location", d.location),
                ("Start date", d.start_date.isoformat() if d.start_date else None),
            ],
        }
        for d in rows
    }


async def client_create_request(
    ctx: RequestContext, data: ClientDemandRequestIn, jd: tuple[bytes, str | None] | None = None
) -> ClientDemandRequestOut:
    """Any active Client User may request a Demand: a Draft owned by the Account Manager (FR-100)."""
    tenant_id, client_id, me = client_of(ctx)
    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "client_user", me)
        client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
        if client is None:
            raise NotFound("Client not found")
        settings = await demand_settings(ctx, db)
        req_id = await next_human_id(db, REQ_SEQUENCE_KEY, prefix=settings.req_prefix, width=4)
        demand = Demand(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            req_id=req_id,
            title=data.title,
            client_id=client_id,
            positions=data.positions,
            location=data.location,
            start_date=data.start_date,
            bill_rate=data.budget_amount,
            currency=((data.budget_currency or "INR").upper() if data.budget_amount is not None else None),
            rate_unit=data.budget_unit if data.budget_amount is not None else None,
            status="draft",
            owner_id=client.owner_id,
        )
        db.add(demand)
        await db.flush()
        await db.execute(
            update(_demands).where(_demands.c.id == demand.id).values(requested_by_client_user_id=me)
        )
        if data.skills:
            await _set_skills(ctx, db, demand, data.skills)
        jd_attached = False
        if jd is not None:
            await _attach_jd(ctx, db, demand, jd[0], jd[1])
            jd_attached = True
        if data.description:
            from app.services.demands.common import timeline

            await timeline(ctx, db, demand.id, f"Demand {req_id} requested through the Client Portal")
        emit(
            db,
            ClientDemandRequested(
                tenant_id=tenant_id,
                demand_id=demand.id,
                client_id=client_id,
                client_user_id=me,
                owner_id=client.owner_id,
            ),
        )
        await _notify_owner(db, demand, data.description)
        return ClientDemandRequestOut(
            id=demand.id,
            req_id=demand.req_id,
            title=demand.title,
            status=demand.status,
            jd_attached=jd_attached,
        )


async def _set_skills(ctx: RequestContext, db: AsyncSession, demand: Demand, skills: list[str]) -> None:
    from app.schemas.demands import DemandSkillIn
    from app.services.demands.records import set_skills

    try:
        async with db.begin_nested():
            await set_skills(
                ctx, db, demand, [DemandSkillIn(label=s[:200], kind="mandatory") for s in skills if s.strip()]
            )
    except Exception:
        logger.warning("Client demand request: skills not recorded for {}", demand.id)


async def _attach_jd(
    ctx: RequestContext, db: AsyncSession, demand: Demand, data: bytes, filename: str | None
) -> None:
    from app.services import files_service

    stored = await files_service.store_upload(ctx, db, data, filename, JD_KINDS, "demand", demand.id)
    db.add(
        DemandDocument(
            id=uuid.uuid4(),
            tenant_id=demand.tenant_id,
            demand_id=demand.id,
            kind="attachment",
            file_name=stored.original_name,
            content_type=stored.content_type,
            size=stored.size_bytes,
            storage_key=stored.storage_key,
            scan_status=stored.scan_status,
            file_id=stored.id,
        )
    )
    await db.flush()


async def _notify_owner(db: AsyncSession, demand: Demand, description: str | None) -> None:
    from app.services import notifications_service as ns

    try:
        await ns.deliver(
            db,
            "client_portal.demand_requested",
            [demand.owner_id],
            ns.Content(
                title=f"New demand request from the client: {demand.title}"[:300],
                body=(description or "")[:2000] or None,
                entity_type="demand",
                entity_id=demand.id,
                link_path=f"/demands/{demand.id}",
            ),
            event_id=f"client_portal.demand_requested:{demand.id}",
        )
    except KeyError:  # hooks not registered (unit contexts)
        logger.debug("client_portal.demand_requested not registered")
