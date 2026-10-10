"""List envelope ``{items, total, page, page_size}`` (AD-12)."""

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

MAX_PAGE_SIZE = 100
MAX_PAGE = 1_000_000  # keeps (page - 1) * page_size far inside int64
DEFAULT_PAGE_SIZE = 20


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class PageParams:
    """Use as ``params: Annotated[PageParams, Depends()]``."""

    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


async def paginate(session: AsyncSession, stmt: Select[Any], params: PageParams) -> Page[Any]:
    """Run ``stmt`` for one page. Returns ORM objects (single-entity selects) or rows.

    Callers map ``items`` to their response schema.
    """
    if not stmt._order_by_clauses:
        msg = "paginate() requires an ORDER BY for deterministic pages"
        raise ValueError(msg)
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await session.execute(count_stmt)).scalar_one())
    result = await session.execute(stmt.limit(params.page_size).offset(params.offset))
    single_entity = len(stmt.column_descriptions) == 1
    items: list[Any] = list(result.scalars().all()) if single_entity else list(result.all())
    return Page[Any](items=items, total=total, page=params.page, page_size=params.page_size)
