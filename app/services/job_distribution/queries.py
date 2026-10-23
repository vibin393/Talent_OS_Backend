# ruff: file-ignore[line-too-long]
"""Read helpers: a Board Posting's applicants (ingest rows, no personal data) and a board's field schema."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.job_distribution import BoardApplicantIngest
from app.schemas.job_distribution import ApplicantRowOut, BoardFieldOut
from app.services import integrations_service
from app.services.job_distribution import mapping
from app.services.job_distribution.board_sets import schema_out
from app.services.job_distribution.common import board_installation
from app.services.job_distribution.postings import _bp_for


async def applicants(ctx: RequestContext, db: AsyncSession, board_posting_id: uuid.UUID) -> list[ApplicantRowOut]:
    bp = await _bp_for(ctx, db, board_posting_id, "demand:read")
    rows = (
        await db.execute(
            select(BoardApplicantIngest)
            .where(BoardApplicantIngest.board_posting_id == bp.id)
            .order_by(BoardApplicantIngest.received_at.desc())
        )
    ).scalars()
    return [ApplicantRowOut.model_validate(r) for r in rows]


async def board_fields(ctx: RequestContext, db: AsyncSession, board_key: str) -> list[BoardFieldOut]:
    pair = await board_installation(db, board_key)
    if pair is None:
        raise NotFound("The board is not installed", code="board_not_installed")
    adapter = await integrations_service.adapter_for(ctx, db, pair[0].id, config_id=pair[1].id)
    return schema_out(await mapping.field_schema(adapter, pair[0].id))
