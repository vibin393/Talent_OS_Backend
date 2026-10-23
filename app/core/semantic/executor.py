"""Read-only execution of compiled semantic queries (Epic 36 contract 0.6).

Every statement runs inside a SAVEPOINT with ``transaction_read_only = on`` and ``statement_timeout``
(``STATEMENT_TIMEOUT``, 30 s) set locally; the savepoint is rolled back after reading, which restores both
settings, so the caller's outer transaction stays writable. A cancelled statement raises ``SemanticTimeout``
(503 ``query_timeout``).
"""

import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.fiscal import fiscal_year
from app.core.semantic import errors as E  # ruff: ignore[lowercase-imported-as-non-lowercase]
from app.core.semantic.compiler import CompiledQuery, SemanticContext
from app.core.semantic.compiler import compile as compile_definition
from app.core.semantic.errors import SemanticError, SemanticTimeout
from app.core.semantic.schema import Definition
from app.schemas.semantic import ColumnMeta

__all__ = [
    "MAX_EXECUTE_LIMIT",
    "MAX_PREVIEW_ROWS",
    "STATEMENT_TIMEOUT",
    "SemanticResult",
    "count",
    "execute",
    "ids",
    "read_only",
]

STATEMENT_TIMEOUT = "30s"  # module constant so tests can shorten it
MAX_PREVIEW_ROWS = 500
MAX_EXECUTE_LIMIT = 100_000
_QUERY_CANCELED = "57014"


@dataclass(frozen=True)
class SemanticResult:
    columns: list[ColumnMeta]
    rows: list[dict[str, Any]]
    total: int | None
    truncated: bool
    masked_fields: list[str]


def _sqlstate(exc: BaseException) -> str | None:
    todo: list[BaseException | None] = [exc]
    seen: list[BaseException] = []
    while todo:
        cur = todo.pop()
        if cur is None or any(cur is s for s in seen):
            continue
        seen.append(cur)
        code = getattr(cur, "sqlstate", None) or getattr(cur, "pgcode", None)
        if isinstance(code, str) and code:
            return code
        todo.extend([getattr(cur, "orig", None), cur.__cause__, cur.__context__])
    return None


@contextlib.asynccontextmanager
async def read_only(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """SAVEPOINT + local read-only + statement timeout; always rolled back afterwards."""
    savepoint = await db.begin_nested()
    try:
        await db.execute(
            text(
                "SELECT set_config('transaction_read_only', 'on', true),"
                " set_config('statement_timeout', :timeout, true)"
            ),
            {"timeout": STATEMENT_TIMEOUT},
        )
        yield db
    except DBAPIError as exc:
        if _sqlstate(exc) == _QUERY_CANCELED:
            raise SemanticTimeout() from exc
        raise
    finally:
        if savepoint.is_active:
            await savepoint.rollback()


def _value(label: str, value: Any, fy_labels: frozenset[str]) -> Any:
    if label in fy_labels and value is not None:
        return fiscal_year(value.date() if isinstance(value, datetime) else value)
    return value


async def execute(
    cq: CompiledQuery, ctx: SemanticContext, *, limit: int = MAX_PREVIEW_ROWS, offset: int = 0
) -> SemanticResult:
    """Run a compiled query (rows ``offset .. offset+limit``; ``total`` counts all result rows)."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_EXECUTE_LIMIT:
        raise SemanticError(E.TOO_COMPLEX, f"limit must be 1 to {MAX_EXECUTE_LIMIT}", "limit")
    if offset < 0:
        raise SemanticError(E.VALUE_TYPE, "offset must be >= 0", "offset")
    stmt: Select[Any] = cq.stmt.limit(limit).offset(offset)
    count_stmt = select(func.count()).select_from(cq.stmt.order_by(None).subquery())
    async with read_only(ctx.db) as db:
        raw = (await db.execute(stmt)).mappings().all()
        total = int((await db.execute(count_stmt)).scalar_one())
    rows: list[dict[str, Any]] = []
    for r in raw:
        row: dict[str, Any] = {}
        if "subject_id" in r:
            row["subject_id"] = r["subject_id"]
        for label, key in cq.labels.items():
            row[key] = _value(label, r[label], cq.fy_labels)
        rows.append(row)
    return SemanticResult(
        columns=list(cq.columns),
        rows=rows,
        total=total,
        truncated=total > offset + len(rows),
        masked_fields=list(cq.masked_fields),
    )


async def ids(defn: Definition, ctx: SemanticContext, *, limit: int | None = None) -> list[uuid.UUID]:
    """Distinct subject ids matching ``defn`` (ascending)."""
    cq = await compile_definition(defn, ctx)
    assert cq.ids_stmt is not None
    stmt = cq.ids_stmt if limit is None else cq.ids_stmt.limit(max(0, limit))
    async with read_only(ctx.db) as db:
        return list((await db.execute(stmt)).scalars().all())


async def count(defn: Definition, ctx: SemanticContext) -> int:
    """Count the distinct subjects matching ``defn``."""
    cq = await compile_definition(defn, ctx)
    assert cq.count_stmt is not None
    async with read_only(ctx.db) as db:
        return int((await db.execute(cq.count_stmt)).scalar_one())
