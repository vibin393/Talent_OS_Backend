# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Shared Public API adapter helpers (Epic 67, spec 0.6).

* ``page`` / ``one``: keyset lists on ``(updated_at | created_at, id)`` (ascending, ``order=desc`` reverses) and
  single reads where an out-of-scope id is the same 404 as a missing one (NFR-60).
* ``finish``: explicit output dicts -> ``mask_for_scopes`` groups (``pii.*`` scopes) on top of the user's own masking
  (compensation needs ``compensation:view``, identity ``identity:view``); masked fields are ``null`` and listed in
  ``masked_fields``.
* ``expose``: ``subject_exposures`` for Candidates / Workers in a response (App: installation; API key: key).
"""

import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_context
from app.core.errors import NotFound, ValidationFailed
from app.core.permissions import Principal
from app.core.public_api import FIELD_GROUPS, decode_cursor, encode_cursor, invalid_cursor, masked_fields
from app.services import exposure_service

Order = Literal["asc", "desc"]


def ctx_principal() -> tuple[RequestContext, Principal]:
    ctx = get_context()
    principal = ctx.principal
    if not isinstance(principal, Principal):  # pragma: no cover - routes always authenticate
        raise NotFound()
    return ctx, principal


def not_found() -> NotFound:
    return NotFound("Resource not found")


def version_of(row: Any) -> str:
    for attr in ("version", "lock_version"):
        value = getattr(row, attr, None)
        if isinstance(value, int):
            return str(value)
    stamp = getattr(row, "updated_at", None) or getattr(row, "created_at", None)
    return str(int(stamp.timestamp() * 1_000_000)) if isinstance(stamp, datetime) else "0"


def base(row: Any, obj: str, *, human_id: str | None = None) -> dict[str, Any]:
    created = getattr(row, "created_at", None)
    return {
        "id": row.id,
        "object": obj,
        "human_id": human_id,
        "created_at": created,
        "updated_at": getattr(row, "updated_at", None) or created,
        "version": version_of(row),
    }


def money(amount: Decimal | int | float | None, currency: str | None) -> dict[str, str] | None:
    if amount is None:
        return None
    return {"amount": format(Decimal(str(amount)), "f"), "currency": currency or "INR"}


def hidden_fields(principal: Principal) -> frozenset[str]:
    """Scope masking (``pii.*``) plus the user's own masking (a delegated user without ``compensation:view`` /
    ``identity:view`` never sees those groups, whatever the token's scopes)."""
    hidden = set(masked_fields(principal))
    if not principal.has("compensation:view"):
        hidden |= FIELD_GROUPS["financial"]
    if not principal.has("identity:view"):
        hidden |= FIELD_GROUPS["identity"]
    return frozenset(hidden)


def finish(principal: Principal, data: dict[str, Any], *, hidden: frozenset[str] | None = None) -> dict[str, Any]:
    hidden = hidden if hidden is not None else hidden_fields(principal)
    listed: list[str] = []

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: (None if k in hidden else walk(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in hidden and key not in ("id", "object"):
            out[key] = None
            listed.append(key)
        else:
            out[key] = walk(value)
    out["masked_fields"] = sorted(listed)
    return out


def finish_all(principal: Principal, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    hidden = hidden_fields(principal)
    return [finish(principal, r, hidden=hidden) for r in rows]


async def expose(principal: Principal, subject_type: str, ids: Iterable[uuid.UUID]) -> None:
    inst = principal.app_installation_id if principal.type == "app" else None
    key = principal.api_key_id if principal.type == "api_key" else None
    if inst is None and key is None:
        return
    ctx = get_context()
    for sid in dict.fromkeys(i for i in ids if i is not None):
        await exposure_service.record(ctx, subject_type=subject_type, subject_id=sid, channel="api",
                                      installation_id=inst, api_key_id=key if inst is None else None)  # fmt: skip


async def page(
    db: AsyncSession,
    stmt: Select[Any],
    *,
    sort_col: Any,
    id_col: Any,
    limit: int,
    cursor: str | None,
    order: Order,
    build: Callable[[Sequence[Any]], Awaitable[list[dict[str, Any]]]],
) -> dict[str, Any]:
    """Keyset page (``(sort, id)``; stable under inserts). ``build`` maps the fetched rows to output dicts."""
    if order not in ("asc", "desc"):
        raise ValidationFailed("order must be asc or desc", details={"fields": {"order": "asc | desc"}})
    if cursor:
        values, last_id = decode_cursor(cursor)
        if len(values) != 1:
            raise invalid_cursor()
        cmp = tuple_(sort_col, id_col) > tuple_(values[0], last_id) if order == "asc" else tuple_(sort_col, id_col) < tuple_(values[0], last_id)  # type: ignore[arg-type]
        stmt = stmt.where(cmp)
    ordering = (sort_col.asc(), id_col.asc()) if order == "asc" else (sort_col.desc(), id_col.desc())
    rows = list((await db.execute(stmt.order_by(*ordering).limit(limit + 1))).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    sort_name = sort_col.key
    next_cursor = encode_cursor((getattr(rows[-1], sort_name),), rows[-1].id) if has_more and rows else None
    return {"data": await build(rows), "next_cursor": next_cursor, "has_more": has_more}


async def one(db: AsyncSession, stmt: Select[Any]) -> Any:
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        raise not_found()
    return row


def acting_owner(principal: Principal, requested: uuid.UUID | None, field: str = "owner_id") -> uuid.UUID:
    """The owner of a record created through the API: the delegated user, else the explicit ``owner_id`` (app-only
    tokens and API keys own nothing themselves)."""
    from app.core.actors import acting_user_id

    owner = requested or acting_user_id(principal)
    if owner is None:
        raise ValidationFailed(f"{field} is required for app access", details={"fields": {field: "Required"}})
    return owner


__all__ = [
    "Order",
    "acting_owner",
    "base",
    "ctx_principal",
    "expose",
    "finish",
    "finish_all",
    "hidden_fields",
    "money",
    "not_found",
    "one",
    "page",
    "version_of",
]
