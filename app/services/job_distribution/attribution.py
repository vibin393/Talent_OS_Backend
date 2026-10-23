# ruff: file-ignore[line-too-long]
"""Careers attribution resolvers (Epic 70, spec A6; D-18): ``src=<board>&bp=<board posting id>`` on the careers apply
link -> ``(source="job_board", source_ref=<board posting id>)`` when ``bp`` is a Board Posting of this Tenant on that
board (open or closed); anything else is ignored (the application stays ``career_portal``)."""

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job_distribution import BoardPosting

ATTRIBUTED_BOARDS = ("naukri", "linkedin", "indeed", "job_feed", "fake_job_board")


def resolver(board_key: str) -> Callable[[AsyncSession, uuid.UUID, str | None], Awaitable[tuple[str, str] | None]]:
    async def resolve(db: AsyncSession, tenant_id: uuid.UUID, ref: str | None) -> tuple[str, str] | None:
        try:
            bp_id = uuid.UUID(str(ref))
        except (TypeError, ValueError):
            return None
        hit = (
            await db.execute(
                select(BoardPosting.id).where(
                    BoardPosting.id == bp_id, BoardPosting.tenant_id == tenant_id, BoardPosting.board_key == board_key
                )
            )
        ).scalar_one_or_none()
        return ("job_board", str(hit)) if hit is not None else None

    return resolve


def register() -> None:
    from app.services.portal.apply import register_apply_source_resolver

    for key in ATTRIBUTED_BOARDS:
        register_apply_source_resolver(key, resolver(key))
