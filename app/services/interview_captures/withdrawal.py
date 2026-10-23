# ruff: file-ignore[line-too-long]
"""Withdrawal, sweep and retention of interview captures (Epic 60, A7).

``ConsentWithdrawn(purpose="interview_recording")`` or an interviewer's acknowledgement revocation -> task
``interview_captures.withdraw``: the recording is purged (``recording_deleted_at``), the transcript erased, the outputs
``erased`` (summaries and quotes removed), drafts ``superseded`` unless already applied to a submitted scorecard (then
only their evidence quotes are removed), pending P3 suggestions from the capture rejected by the system, and the hub row
``erased``. The hourly sweep enforces the 24 h SLA for anything a failed task missed.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.models.interview_captures import InterviewCapture, InterviewCaptureOutput, InterviewScorecardDraft
from app.models.transcripts import Transcript
from app.services.interview_captures import common, skill_evidence

ACTIVE = ("uploaded", "processing", "ready")


def schedule(tenant_id: uuid.UUID, capture_id: uuid.UUID, *, reason: str) -> None:
    from app.tasks import interview_captures as tasks
    from app.tasks.runtime import enqueue

    try:
        enqueue(tasks.withdraw, str(tenant_id), str(capture_id), reason)
    except Exception:
        logger.exception("Could not enqueue the interview capture withdrawal")


async def scrub_outputs(db: AsyncSession, capture_ids: Sequence[uuid.UUID]) -> int:
    ids = list(capture_ids)
    if not ids:
        return 0
    result = await db.execute(
        update(InterviewCaptureOutput)
        .where(InterviewCaptureOutput.capture_id.in_(ids))
        .values(status="erased", summary_by_competency=[], qa_items=[], overall_summary=None)
    )
    drafts = list(
        (
            await db.execute(
                select(InterviewScorecardDraft).where(InterviewScorecardDraft.capture_id.in_(ids))
            )
        ).scalars()
    )
    for d in drafts:
        d.ratings = [{**r, "evidence": [], "rationale": ""} for r in d.ratings or []]
        d.answers = []
        if d.status != "applied":
            d.status = "superseded"
    await db.flush()
    return int(getattr(result, "rowcount", 0) or 0)


async def withdraw(db: AsyncSession, capture_id: uuid.UUID, *, reason: str) -> dict[str, int]:
    from app.services import files_service
    from app.services.transcripts.service import erase_rows

    capture = await db.get(InterviewCapture, capture_id, with_for_update=True)
    if capture is None or capture.status == "erased":
        return {"withdrawn": 0}
    purged = 0
    if capture.file_id is not None:
        await files_service.purge_file(db, capture.file_id)
        capture.file_id = None
        purged = 1
    capture.recording_deleted_at = common.now()
    capture.recording_allowed = False
    erased = 0
    if capture.transcript_id is not None:
        rows = list(
            (await db.execute(select(Transcript).where(Transcript.source_id == capture.id))).scalars()
        )
        erased = await erase_rows(db, rows, reason=f"withdrawal:{reason}")
    outputs = await scrub_outputs(db, [capture.id])
    rejected = await skill_evidence.reject_pending(db, capture.id)
    capture.status = "erased"
    capture.speakers = []
    await db.flush()
    if capture.agent_run_id is not None:
        await _cancel_run(db, capture)
    audit.record(
        "interview_capture.withdrawn", "interview", capture.interview_id, session=db, actor_type="system", actor_id=None,
        reason=reason, recording_purged=purged,
    )  # fmt: skip
    return {
        "withdrawn": 1,
        "recording": purged,
        "segments": erased,
        "outputs": outputs,
        "suggestions": rejected,
    }


async def _cancel_run(db: AsyncSession, capture: InterviewCapture) -> None:
    from app.core.context import RequestContext
    from app.services import agent_runtime_service

    try:
        await agent_runtime_service.cancel_run(
            RequestContext(tenant_id=capture.tenant_id, actor_kind="system"),
            db,
            capture.agent_run_id,
            reason="interview recording consent withdrawn",
            reason_code="subject_closed",
        )  # type: ignore[arg-type]
    except Exception:
        logger.debug("Interview Assistant run {} not cancelled", capture.agent_run_id)


async def for_candidate(db: AsyncSession, candidate_id: uuid.UUID, *, reason: str) -> int:
    rows = list(
        (
            await db.execute(
                select(InterviewCapture.id).where(
                    InterviewCapture.candidate_id == candidate_id, InterviewCapture.status != "erased"
                )
            )
        ).scalars()
    )
    n = 0
    for cid in rows:
        capture = await db.get(InterviewCapture, cid)
        if capture is None:
            continue
        if capture.status in ACTIVE:
            n += (await withdraw(db, cid, reason=reason))["withdrawn"]
        else:
            interview = await common.interview_row(db, capture.interview_id)
            from app.services.interview_captures import consent

            await consent.refresh(db, interview, capture)
    return n


async def sweep(db: AsyncSession) -> int:
    """Hourly: any active capture whose consent / acknowledgements no longer hold is withdrawn (24 h SLA)."""
    from app.services.interview_captures import consent

    n = 0
    rows = list(
        (await db.execute(select(InterviewCapture).where(InterviewCapture.status.in_(ACTIVE)))).scalars()
    )
    for capture in rows:
        interview = await common.interview_row(db, capture.interview_id)
        ev = await consent.evaluate(db, interview, capture)
        consent_gone = any(
            r == "missing_consent:candidate" or r.startswith("missing_ack:") for r in ev.reasons
        )
        if consent_gone:
            n += (await withdraw(db, capture.id, reason="sweep"))["withdrawn"]
    return n


async def expire_outputs(
    db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "delete"
) -> dict[str, int]:
    """Retention (``conversation_records``): erase summaries and quotes of expired outputs."""
    ids = list(ids)
    if not ids:
        return {"deleted": 0}
    capture_ids = list(
        (
            await db.execute(
                select(InterviewCaptureOutput.capture_id).where(InterviewCaptureOutput.id.in_(ids))
            )
        ).scalars()
    )
    await scrub_outputs(db, capture_ids)
    return {"deleted": len(ids)}


async def output_subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
    return (
        await db.execute(
            select(InterviewCaptureOutput.candidate_id).where(InterviewCaptureOutput.id == row_id)
        )
    ).scalar_one_or_none()


async def output_anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
    return (
        await db.execute(select(InterviewCaptureOutput.created_at).where(InterviewCaptureOutput.id == row_id))
    ).scalar_one_or_none()


__all__ = ["expire_outputs", "for_candidate", "schedule", "scrub_outputs", "sweep", "withdraw"]
