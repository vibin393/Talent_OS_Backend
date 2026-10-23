# ruff: file-ignore[line-too-long]
"""Human override of the Collections Agent (Epic 63 A4, FR-315): pause per Client or document, take over a document
thread, release. ``collections:write``; audited (``AuditedMixin``); emits ``CollectionsAgentHoldChanged`` and writes the
collection log (``held`` / ``takeover`` / ``released``). ``is_held`` / ``assert_not_held`` guard every agent send
(``ConflictError collections_agent_paused``).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, NotFound
from app.core.events import emit
from app.events.collection_rules import CollectionsAgentHoldChanged
from app.models.clients import Client
from app.models.collection_rules import CollectionAgentHold
from app.schemas.collection_rules import AgentStateOut, HoldIn, HoldOut
from app.services.collection_rules import log


def _uid(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return getattr(p, "id", None) if p is not None and getattr(p, "type", "") == "user" else None


def _require(ctx: RequestContext) -> None:
    p = ctx.principal
    if p is None or not p.has("collections:write"):
        raise Forbidden("You do not have permission to pause the collections agent", details={"permission": "collections:write"})


async def active_holds(db: AsyncSession, client_id: uuid.UUID) -> list[CollectionAgentHold]:
    return list(
        (
            await db.execute(
                select(CollectionAgentHold).where(
                    CollectionAgentHold.client_id == client_id, CollectionAgentHold.released_at.is_(None)
                ).order_by(CollectionAgentHold.created_at)
            )
        ).scalars()
    )  # fmt: skip


async def hold(ctx: RequestContext, db: AsyncSession, data: HoldIn) -> HoldOut:
    _require(ctx)
    if await db.get(Client, data.client_id) is None:
        raise NotFound("Client not found")
    if data.document_id is not None:
        from app.services import receivables_service

        docs = await receivables_service.collectible_documents(db, data.client_id, ids=[data.document_id])
        if not docs:
            raise NotFound("Invoice not found")
    row = CollectionAgentHold(
        id=uuid.uuid4(), tenant_id=_tenant(db), client_id=data.client_id, document_kind=data.document_kind,
        document_id=data.document_id, mode=data.mode, reason=(data.reason or "").strip()[:500] or None, created_by=_uid(ctx),
    )  # fmt: skip
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError("The agent is already paused here", code="hold_exists") from exc
    await db.refresh(row)
    actor = _uid(ctx)
    await log.write(
        db, client_id=data.client_id, document_ids=[data.document_id] if data.document_id else [],
        event="takeover" if data.mode == "takeover" else "held", actor={"kind": "user", "id": str(actor) if actor else None},
        summary=("Taken over" if data.mode == "takeover" else "Paused") + (" (invoice)" if data.document_id else " (client)"),
    )  # fmt: skip
    if actor is not None:
        emit(db, CollectionsAgentHoldChanged(
            tenant_id=row.tenant_id, client_id=row.client_id, document_id=row.document_id, mode=row.mode, active=True, actor_id=actor,
        ))  # fmt: skip
    return HoldOut.model_validate(row)


async def release(ctx: RequestContext, db: AsyncSession, hold_id: uuid.UUID) -> HoldOut:
    _require(ctx)
    row = (
        await db.execute(select(CollectionAgentHold).where(CollectionAgentHold.id == hold_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Hold not found")
    if row.released_at is None:
        row.released_at = datetime.now(UTC)
        row.released_by = _uid(ctx)
        await db.flush()
        actor = _uid(ctx)
        await log.write(
            db, client_id=row.client_id, document_ids=[row.document_id] if row.document_id else [], event="released",
            actor={"kind": "user", "id": str(actor) if actor else None}, summary="Handed back to the agent",
        )  # fmt: skip
        if actor is not None:
            emit(db, CollectionsAgentHoldChanged(
                tenant_id=row.tenant_id, client_id=row.client_id, document_id=row.document_id, mode=row.mode, active=False, actor_id=actor,
            ))  # fmt: skip
    return HoldOut.model_validate(row)


async def list_holds(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, *, include_released: bool = False) -> list[HoldOut]:  # ruff: ignore[unused-function-argument]
    stmt = select(CollectionAgentHold).where(CollectionAgentHold.client_id == client_id)
    if not include_released:
        stmt = stmt.where(CollectionAgentHold.released_at.is_(None))
    return [HoldOut.model_validate(r) for r in (await db.execute(stmt.order_by(CollectionAgentHold.created_at.desc()))).scalars()]


async def state(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID) -> AgentStateOut:  # ruff: ignore[unused-function-argument]
    rows = await active_holds(db, client_id)
    client_paused = any(r.document_id is None for r in rows)
    value = "paused" if client_paused else ("taken_over" if any(r.mode == "takeover" for r in rows) else "active")
    return AgentStateOut(client_id=client_id, state=value, holds=[HoldOut.model_validate(r) for r in rows])


async def holds_map(db: AsyncSession, client_id: uuid.UUID) -> tuple[str | None, dict[uuid.UUID, str]]:
    rows = await active_holds(db, client_id)
    client = next((r.mode for r in rows if r.document_id is None), None)
    return client, {r.document_id: r.mode for r in rows if r.document_id is not None}


async def is_held(db: AsyncSession, client_id: uuid.UUID, document_ids: list[uuid.UUID] | None = None) -> bool:
    client, docs = await holds_map(db, client_id)
    return client is not None or any(d in docs for d in document_ids or [])


async def assert_not_held(db: AsyncSession, client_id: uuid.UUID, document_ids: list[uuid.UUID] | None = None) -> None:
    if await is_held(db, client_id, document_ids):
        raise ConflictError(
            "The collections agent is paused or taken over for this client or invoice", code="collections_agent_paused"
        )


def _tenant(db: AsyncSession) -> uuid.UUID:
    from app.services import entity_refs

    return entity_refs.session_tenant(db)


__all__ = ["active_holds", "assert_not_held", "hold", "holds_map", "is_held", "list_holds", "release", "state"]
