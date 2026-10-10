# ruff: noqa: ARG001, N812
"""Shared helpers of the Phase 2 tile functions (Epic 34, A3).

Row visibility: placement-bound rows use the placements domain's own rule (``placement:read`` scope on the
recruiter OR the account manager); Client-bound finance rows use ``billing:read`` scope on the Client owner.
Every money value is the AD-12 shape ``{"amount": "…", "currency": "INR"}`` (Decimal strings).
"""

import uuid
from typing import Any

from sqlalchemy import ColumnElement, or_, select

from app.core.permissions import Principal, can_view_compensation, scope_condition
from app.models.clients import Client
from app.services.reporting import p2tables as T
from app.services.reporting.metrics import TileContext, link
from app.services.reporting.summaries import NotAvailable

P = T.placements.c


def need(t: TileContext, *tables: str) -> None:
    missing = [n for n in tables if n not in t.avail]
    if missing:
        raise NotAvailable(",".join(missing))


def placement_scope(principal: Principal) -> ColumnElement[bool] | None:
    a = scope_condition(principal, "placement:read", P.recruiter_user_id)
    if a is None:
        return None
    b = scope_condition(principal, "placement:read", P.account_manager_user_id)
    return or_(a, b) if b is not None else a


def scoped_placements(t: TileContext, *conds: Any) -> Any:
    """``SELECT placements.id`` visible to the caller, narrowed to the LE filter and ``conds``."""
    stmt = select(P.id).where(*conds)
    cond = placement_scope(t.principal)
    if cond is not None:
        stmt = stmt.where(cond)
    if t.filters.legal_entity_ids:
        stmt = stmt.where(P.legal_entity_id.in_(t.filters.legal_entity_ids))
    return stmt


def client_scope(t: TileContext, client_col: Any) -> ColumnElement[bool] | None:
    cond = scope_condition(t.principal, "billing:read", Client.owner_id)
    if cond is None:
        return None
    return client_col.in_(select(Client.id).where(cond))


def le_cond(t: TileContext, col: Any) -> list[Any]:
    return [col.in_(t.filters.legal_entity_ids)] if t.filters.legal_entity_ids else []


def comp(t: TileContext) -> bool:
    return can_view_compensation(t.principal)


def le_link(t: TileContext, path: str, **query: Any) -> dict[str, Any]:
    return link(path, {**t.filters.le_query(), **query})


def client_names(rows: list[Any]) -> Any:
    return select(Client.id, Client.name)


async def names(t: TileContext, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await t.db.execute(select(Client.id, Client.name).where(Client.id.in_(ids)))
    return {k: str(v) for k, v in rows.tuples()}
