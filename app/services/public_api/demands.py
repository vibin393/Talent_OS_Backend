# ruff: noqa: E501
"""Public Demands and Postings (Epic 67, spec A4). Reads use ``demands.common.access_condition`` (owner / BU scope
plus assignments). ``POST /demands`` creates a draft only (``records.create_demand_row``); submit / approve / open stay
in the web app (★). Postings publish / close to the careers site through ``demands.jd.publish`` / ``unpublish``: the
posting re-publishes its JD version and still needs an Open demand (409 otherwise)."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.demands import Demand, DemandPosting
from app.schemas.demands import DemandCreate, PostingIn
from app.schemas.public_v1.resources import DemandCreateIn
from app.services.demands import jd, records
from app.services.demands.common import access_condition
from app.services.public_api._core import (
    Order,
    acting_owner,
    base,
    ctx_principal,
    finish_all,
    money,
    one,
    page,
)

READ = "demand:read"


def _visible() -> Any:
    _, principal = ctx_principal()
    stmt = select(Demand).where(Demand.archived_at.is_(None)) if hasattr(Demand, "archived_at") else select(Demand)
    cond = access_condition(principal, READ)
    return stmt if cond is None else stmt.where(cond)


async def _build(rows: Sequence[Demand]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    return finish_all(principal, [
        {**base(d, "demand", human_id=d.req_id), "title": d.title, "client_id": d.client_id, "status": d.status,
         "positions": d.positions, "location": d.location, "employment_type": d.employment_type, "work_mode": d.work_mode,
         "priority": d.priority, "owner_id": d.owner_id, "start_date": d.start_date, "target_closure_date": d.target_closure_date,
         "bill_rate": money(d.bill_rate, d.currency)}
        for d in rows
    ])  # fmt: skip


async def list_demands(db: AsyncSession, *, limit: int, cursor: str | None, order: Order, status: str | None = None, client_id: uuid.UUID | None = None) -> dict[str, Any]:
    stmt = _visible()
    if status is not None:
        stmt = stmt.where(Demand.status == status)
    if client_id is not None:
        stmt = stmt.where(Demand.client_id == client_id)
    return await page(db, stmt, sort_col=Demand.updated_at, id_col=Demand.id, limit=limit, cursor=cursor, order=order, build=_build)


async def get_demand(db: AsyncSession, demand_id: uuid.UUID) -> dict[str, Any]:
    return (await _build([await one(db, _visible().where(Demand.id == demand_id))]))[0]


async def create_demand(db: AsyncSession, data: DemandCreateIn) -> dict[str, Any]:
    ctx, principal = ctx_principal()
    owner = acting_owner(principal, data.owner_id)
    body = DemandCreate(title=data.title, client_id=data.client_id, positions=data.positions,
                        **({"employment_type": data.employment_type} if data.employment_type else {}))  # fmt: skip
    demand = await records.create_demand_row(ctx, db, body, actor_kind=principal.type, owner_id=owner)
    await db.flush()
    return (await _build([demand]))[0]


# ================================================================ postings
def _postings() -> Any:
    return select(DemandPosting).where(DemandPosting.demand_id.in_(_visible().with_only_columns(Demand.id)))


async def _build_postings(rows: Sequence[DemandPosting]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    return finish_all(principal, [
        {**base(p, "posting"), "demand_id": p.demand_id, "slug": p.slug, "is_live": p.is_live,
         "published_at": p.published_at, "unpublished_at": p.unpublished_at}
        for p in rows
    ])  # fmt: skip


async def list_postings(db: AsyncSession, *, limit: int, cursor: str | None, order: Order, is_live: bool | None = None) -> dict[str, Any]:
    stmt = _postings()
    if is_live is not None:
        stmt = stmt.where(DemandPosting.is_live.is_(is_live))
    return await page(db, stmt, sort_col=DemandPosting.updated_at, id_col=DemandPosting.id, limit=limit, cursor=cursor, order=order, build=_build_postings)


async def get_posting(db: AsyncSession, posting_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_postings([await one(db, _postings().where(DemandPosting.id == posting_id))]))[0]


async def publish_posting(db: AsyncSession, posting_id: uuid.UUID) -> dict[str, Any]:
    ctx, principal = ctx_principal()
    posting = await one(db, _postings().where(DemandPosting.id == posting_id))
    await jd.publish(ctx, db, posting.demand_id, PostingIn(
        jd_version_id=posting.jd_version_id, show_rate=posting.show_rate, show_location=posting.show_location,
        show_client_name=posting.show_client_name,
    ), actor_kind=principal.type)  # type: ignore[arg-type]  # fmt: skip
    return await get_posting(db, posting_id)


async def close_posting(db: AsyncSession, posting_id: uuid.UUID) -> dict[str, Any]:
    ctx, principal = ctx_principal()
    posting = await one(db, _postings().where(DemandPosting.id == posting_id))
    await jd.unpublish(ctx, db, posting.demand_id, actor_kind=principal.type)  # type: ignore[arg-type]
    return await get_posting(db, posting_id)


__all__ = ["close_posting", "create_demand", "get_demand", "get_posting", "list_demands", "list_postings", "publish_posting"]
