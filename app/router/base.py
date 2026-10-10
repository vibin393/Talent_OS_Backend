from dataclasses import dataclass

import asyncpg
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.core.database import platform_session

router = APIRouter(prefix="/api", tags=["base"])


@dataclass(frozen=True)
class DBHealth:
    ok: bool
    status_code: int = status.HTTP_200_OK
    error: str | None = None
    db_user: str | None = None


def _root_causes(exc: BaseException) -> list[BaseException]:
    """``exc`` plus what SQLAlchemy/asyncpg wrapped inside it (``.orig`` and ``__cause__`` chains)."""
    seen: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        cur = todo.pop()
        if cur is None or any(cur is s for s in seen):
            continue
        seen.append(cur)
        todo.extend([getattr(cur, "orig", None), cur.__cause__])
    return seen


def _classify(exc: BaseException) -> DBHealth:
    unavailable = status.HTTP_503_SERVICE_UNAVAILABLE
    for cause in _root_causes(exc):
        if isinstance(cause, asyncpg.InvalidCatalogNameError):
            # Note: exposing the DB status might help an attacker guess the DB name; use with caution.
            return DBHealth(False, status.HTTP_404_NOT_FOUND, "Database not found")
        if isinstance(cause, asyncpg.InvalidPasswordError):
            return DBHealth(False, status.HTTP_401_UNAUTHORIZED, "Invalid password")
        if isinstance(cause, ConnectionRefusedError | OSError):
            return DBHealth(False, unavailable, "Connection Refused")
    if isinstance(exc, ProgrammingError | DBAPIError):
        return DBHealth(False, unavailable, "Database connection failed")
    return DBHealth(False, status.HTTP_500_INTERNAL_SERVER_ERROR, "Internal server error")


async def check_database() -> DBHealth:
    """Probe the DB with the runtime role; shared by ``/api/health`` and ``/api/v1/health``."""
    try:
        async with platform_session() as db:
            db_user = (await db.execute(text("SELECT current_user"))).scalar_one()
        return DBHealth(ok=True, db_user=str(db_user))
    except Exception as exc:
        return _classify(exc)


@router.get("/health")
async def health_check() -> JSONResponse:
    health = await check_database()
    if health.ok:
        return JSONResponse(status_code=status.HTTP_200_OK, content={"message": "All Healthy"})
    return JSONResponse(status_code=health.status_code, content={"error": health.error})


@router.get("/pulse")
def pulse() -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_200_OK, content={"message": "I'm Alive"})
