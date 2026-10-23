# ruff: file-ignore[line-too-long]
"""Shared Public API v1 router dependencies (Epic 67, spec A4 / 0.6): scopes, list params, ``Idempotency-Key`` on every
POST (24 h, the request's own public session) and ``If-Match`` on every PATCH (428 / 412)."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request

from app.core.errors import ValidationFailed
from app.core.idempotency import HEADER, MAX_KEY_LENGTH, IdempotencyGuard, request_hash
from app.core.permissions import Principal
from app.core.public_api import check_if_match, etag_for, require_scope
from app.dependencies.public import PublicDBSessionDep


def scope(*keys: str) -> Any:
    return Annotated[Principal, Depends(require_scope(*keys))]


class ListParams:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2000)] = None,
        order: Annotated[Literal["asc", "desc"], Query()] = "asc",
    ) -> None:
        self.limit, self.cursor, self.order = limit, cursor, order

    def kw(self) -> dict[str, Any]:
        return {"limit": self.limit, "cursor": self.cursor, "order": self.order}


Listing = Annotated[ListParams, Depends()]


async def public_idempotency(request: Request, db: PublicDBSessionDep) -> IdempotencyGuard:
    key = (request.headers.get(HEADER) or "").strip() or None
    if key is None:
        raise ValidationFailed("The Idempotency-Key header is required", code="idempotency_key_required")
    if len(key) > MAX_KEY_LENGTH:
        raise ValidationFailed("The Idempotency-Key header is too long", code="idempotency_key_invalid")
    try:
        raw = await request.body()
    except RuntimeError:  # multipart: the form parser consumed the stream; hash its fields and file digests instead
        import hashlib

        form = await request.form()
        parts = []
        for name, value in sorted(form.multi_items(), key=lambda kv: kv[0]):
            if isinstance(value, str):
                parts.append(f"{name}={value}")
            else:
                await value.seek(0)
                parts.append(f"{name}#{hashlib.sha256(await value.read()).hexdigest()}")
                await value.seek(0)
        raw = "&".join(parts).encode()
    route = request.scope.get("route")
    status = int(getattr(route, "status_code", None) or 200)
    return IdempotencyGuard(db, key=key, method=request.method, path=request.url.path,
                            hash_=request_hash(request.method, request.url.path, raw), status_code=status)  # fmt: skip


Idem = Annotated[IdempotencyGuard, Depends(public_idempotency)]


async def guarded_update(request: Request, current: Callable[[], Awaitable[str]], apply: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """``If-Match`` against the current version (428 when missing, 412 when stale), then ``apply``."""
    if request.headers.get("if-match") is None:
        check_if_match(request, "")  # raises 428
    check_if_match(request, await current())
    return await apply()


def etag(obj: dict[str, Any]) -> str:
    return etag_for(obj.get("version", "0"))


__all__ = ["Idem", "Listing", "etag", "guarded_update", "public_idempotency", "scope"]
