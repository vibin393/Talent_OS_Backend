"""AI screening summary (story 6.3, FR-35 / FR-65, AD-6). Runs with ``actor=ai`` from a Celery task.

This module must never import ``transition_application`` or the screening outcome function (an AST
import-graph test enforces it): the AI can summarise and suggest ``proceed`` / ``hold``, never reject.
"""

import json
import uuid

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.pipeline import Screening
from app.schemas.pipeline import ScreeningSummary
from app.services import ai

FEATURE = "ai_screening"
SUMMARY_PROMPT = "screening_summary.v1"


async def summarize(ctx: RequestContext, db: AsyncSession, screening_id: uuid.UUID) -> bool:
    """Summarise an answered AI screening. Idempotent; returns True when a summary was stored.

    The answers go to the model as untrusted input; the stored summary is labelled ``ai_generated``. A
    ``suggested_outcome`` other than proceed / hold never survives validation (see ``ScreeningSummary``).
    """
    row = await db.get(Screening, screening_id, with_for_update=True)
    if row is None or row.kind != "ai" or row.status not in ("answered", "reviewed"):
        return False
    if row.ai_summary is not None:
        return True
    questions = [{"key": q.get("key"), "label": q.get("label"), "type": q.get("type")} for q in row.questions]
    result = await ai.run(
        ctx,
        db,
        FEATURE,
        SUMMARY_PROMPT,
        inputs={},
        untrusted={
            "questions": json.dumps(questions, ensure_ascii=False, indent=1),
            "answers": json.dumps(row.answers, ensure_ascii=False, default=str, indent=1),
        },
        output_schema=ScreeningSummary,
        subject=("candidate", row.candidate_id),
        input_ref=f"screening:{row.id}",
    )
    if not result.ok or not isinstance(result.data, ScreeningSummary):
        row.summary_status = "failed"
        await db.flush()
        logger.info("AI screening summary unavailable (screening={}, error={})", row.id, result.error_code)
        return False
    data = result.data.model_dump(mode="json")
    if data.get("suggested_outcome") not in ("proceed", "hold"):
        data["suggested_outcome"] = None
    data["ai_generated"] = True
    row.ai_summary = data
    row.ai_generated = True
    row.ai_action_id = result.action_id
    row.summary_status = "ready"
    await db.flush()
    return True
