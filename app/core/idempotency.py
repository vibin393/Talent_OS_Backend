"""``Idempotency-Key`` handling for endpoints that issue numbers, move money, lock source items, apply files or
bulk-decide approvals (AD-23, NFR-25).

Usage in a router::

    @router.post("/things", status_code=201)
    async def create(body: ThingIn, db: TenantDBSessionDep, idem: Annotated[IdempotencyGuard, Depends(idempotency())]):
        return await idem.run(lambda: svc.create(get_context(), db, body))

* The key is read from the ``Idempotency-Key`` header (missing + required -> 422 ``idempotency_key_required``).
* ``request_hash`` = SHA-256 of method, path and the canonical JSON body.
* The key row is inserted in the request's own session (same transaction as the business change). A
  concurrent duplicate blocks on the unique index until the first request commits, then re-reads it and
  replays; if the first rolled back, the second proceeds normally.
* Same key + same hash with a stored response -> the stored JSON is replayed with the stored status code.
  Same key + different hash -> 409 ``idempotency_key_reused``. A row without a response older than five
  minutes is treated as abandoned and taken over (younger -> 409 ``idempotency_key_in_progress``).
* Rows expire after 30 days (``maintenance.purge_idempotency_keys``) so a delayed relay/replay still replays
  the stored response rather than re-running the change.

``app.dependencies.auth`` registers the request-session dependency (``set_session_dependency``) so the guard
shares the route's ``TenantDBSessionDep`` session (FastAPI caches a dependency per request).
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import column, delete, select, table, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_context
from app.core.errors import ConflictError, ValidationFailed

HEADER = "Idempotency-Key"
# 30 days (was 24 h): a delayed relay / replay from a spoke app (Croar, Feedback IQ) can arrive long after
# the original call; a short window would let a late retry create a duplicate once the key row had expired.
TTL = timedelta(days=30)
ABANDONED_AFTER = timedelta(minutes=5)
MAX_KEY_LENGTH = 128

_keys = table(
    "idempotency_keys",
    column("id"),
    column("tenant_id"),
    column("principal_type"),
    column("principal_id"),
    column("key"),
    column("request_hash"),
    column("method"),
    column("path"),
    column("status_code"),
    column("response", JSONB()),
    column("expires_at"),
    column("created_at"),
    column("updated_at"),
)

SessionDependency = Callable[..., Any]
_session_dependency: SessionDependency | None = None


def set_session_dependency(dependency: SessionDependency) -> None:
    """Register the request-session dependency (``app.dependencies.auth.get_tenant_db``)."""
    global _session_dependency  # process-wide wiring, set once at import
    _session_dependency = dependency


def _now() -> datetime:
    return datetime.now(UTC)


def canonical_body(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (ValueError, UnicodeDecodeError):
        return raw.hex()


def request_hash(method: str, path: str, raw_body: bytes) -> str:
    material = f"{method.upper()}\n{path}\n{canonical_body(raw_body)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class IdempotencyGuard:
    def __init__(
        self,
        db: AsyncSession,
        *,
        key: str | None,
        method: str,
        path: str,
        hash_: str,
        status_code: int = 200,
    ) -> None:
        self.db = db
        self.key = key
        self.method = method
        self.path = path
        self.hash = hash_
        self.status_code = status_code
        self._row_id: uuid.UUID | None = None

    def _principal(self) -> tuple[str, uuid.UUID]:
        principal = get_context().principal
        kind = getattr(principal, "type", None)
        pid = getattr(principal, "id", None)
        if not kind or pid is None:
            msg = "idempotency needs an authenticated principal"
            raise RuntimeError(msg)
        return str(kind), uuid.UUID(str(pid))

    async def _claim(self) -> JSONResponse | None:
        kind, pid = self._principal()
        tenant_id = self.db.sync_session.info.get("tenant_id")
        now = _now()
        inserted = (
            await self.db.execute(
                pg_insert(_keys)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    principal_type=kind,
                    principal_id=pid,
                    key=self.key,
                    request_hash=self.hash,
                    method=self.method,
                    path=self.path[:500],
                    expires_at=now + TTL,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "principal_type", "principal_id", "key"])
                .returning(_keys.c.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            self._row_id = inserted
            return None
        row = (
            await self.db.execute(
                select(
                    _keys.c.id,
                    _keys.c.request_hash,
                    _keys.c.status_code,
                    _keys.c.response,
                    _keys.c.expires_at,
                    _keys.c.updated_at,
                )
                .where(
                    _keys.c.principal_type == kind, _keys.c.principal_id == pid, _keys.c.key == self.key
                )
                .with_for_update()
            )
        ).first()
        if row is None:  # pragma: no cover - the conflicting row vanished between the two statements
            return await self._claim()
        expired = row.expires_at is not None and row.expires_at <= now
        if not expired and row.request_hash != self.hash:
            raise ConflictError(
                "This Idempotency-Key was already used for a different request", code="idempotency_key_reused"
            )
        if not expired and row.status_code is not None:
            return JSONResponse(
                content=row.response, status_code=int(row.status_code), headers={"Idempotency-Replayed": "true"}
            )
        if not expired and now - row.updated_at < ABANDONED_AFTER:
            raise ConflictError(
                "A request with this Idempotency-Key is still in progress", code="idempotency_key_in_progress"
            )
        # Expired, or abandoned without a response: take it over.
        await self.db.execute(
            update(_keys)
            .where(_keys.c.id == row.id)
            .values(
                request_hash=self.hash,
                method=self.method,
                path=self.path[:500],
                status_code=None,
                response=None,
                expires_at=now + TTL,
                updated_at=now,
            )
        )
        self._row_id = row.id
        return None

    async def run(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        if self.key is None:
            return await fn()
        replay = await self._claim()
        if replay is not None:
            return replay
        result = await fn()
        body = jsonable_encoder(result)
        await self.db.execute(
            update(_keys)
            .where(_keys.c.id == self._row_id)
            .values(status_code=self.status_code, response=body, updated_at=_now())
        )
        return JSONResponse(content=body, status_code=self.status_code)


def _route_status(request: Request) -> int:
    route = request.scope.get("route")
    status = getattr(route, "status_code", None)
    return int(status) if status else 200


def idempotency(required: bool = True) -> Callable[..., Awaitable[IdempotencyGuard]]:
    """Dependency factory returning the request's ``IdempotencyGuard``."""
    session_dependency = _session_dependency
    if session_dependency is None:  # pragma: no cover - wiring error
        msg = "no idempotency session dependency registered (import app.dependencies.auth)"
        raise RuntimeError(msg)

    async def dependency(
        request: Request, db: Annotated[AsyncSession, Depends(session_dependency)]
    ) -> IdempotencyGuard:
        key = (request.headers.get(HEADER) or "").strip() or None
        if key is None and required:
            raise ValidationFailed("The Idempotency-Key header is required", code="idempotency_key_required")
        if key is not None and len(key) > MAX_KEY_LENGTH:
            raise ValidationFailed("The Idempotency-Key header is too long", code="idempotency_key_invalid")
        raw = await request.body()
        return IdempotencyGuard(
            db,
            key=key,
            method=request.method,
            path=request.url.path,
            hash_=request_hash(request.method, request.url.path, raw),
            status_code=_route_status(request),
        )

    return dependency


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    result = await db.execute(delete(_keys).where(_keys.c.expires_at < (now or _now())))
    return int(result.rowcount or 0)
