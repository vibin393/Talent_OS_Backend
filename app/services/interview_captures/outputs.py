# ruff: noqa: E501
"""Summary by competency and question-response mapping (Epic 60, story 60.3; FR-302, FR-303).

``save`` validates the assistant's output against the Interview's kit snapshot (competency and question keys), the
transcript (every quote is a substring of the referenced segment) and the language rule, then stores it as the
capture's single ``ready`` output (a re-run supersedes the previous one) and marks the capture ``ready``. Labels,
question texts and timestamps are filled from the kit and the transcript, never from the model.
"""

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.models.interview_captures import InterviewCapture, InterviewCaptureOutput
from app.services.interview_captures import common, validators

MAX_QA = 60


async def save(
    db: AsyncSession,
    capture: InterviewCapture,
    *,
    summary_by_competency: list[dict[str, Any]],
    qa_items: list[dict[str, Any]],
    overall_summary: str | None,
    run_id: uuid.UUID | None,
) -> InterviewCaptureOutput:
    if capture.status in ("erased", "planned", "consent_pending", "blocked") or capture.transcript_id is None:
        raise common.conflict("capture_not_ready", "There is no consented transcript for this interview")
    interview = await common.interview_row(db, capture.interview_id)
    kit = common.kit_of(interview)
    labels = {c["key"]: c["label"] for c in kit.competencies}
    questions = {q["key"]: q for q in kit.questions}
    segs = await validators.segments(db, capture.transcript_id)
    if len(qa_items) > MAX_QA:
        raise common.invalid("too_many_items", f"At most {MAX_QA} questions")
    validators.check_language(overall_summary)
    summary_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in summary_by_competency:
        key = str(item["competency_key"])
        if key not in labels:
            raise common.invalid(
                "unknown_competency",
                "Use the Kit's competency keys",
                competency_key=key,
                allowed=list(labels),
            )
        if key in seen:
            raise common.invalid("duplicate_competency", "One summary per competency", competency_key=key)
        seen.add(key)
        validators.check_language(item.get("summary"))
        evidence = []
        for ev in item.get("evidence") or []:
            seg = validators.check_quote(segs, int(ev["segment_seq"]), str(ev["quote"]))
            evidence.append(
                {"segment_seq": seg.seq, "start_ms": seg.start_ms, "quote": str(ev["quote"])[:300]}
            )
        summary_rows.append(
            {
                "competency_key": key,
                "label": labels[key],
                "summary": str(item.get("summary") or "")[:1200],
                "evidence": evidence,
            }
        )
    qa_rows: list[dict[str, Any]] = []
    for item in qa_items:
        qkey = item.get("kit_question_key")
        if qkey is not None and qkey not in questions:
            raise common.invalid(
                "unknown_question", "Map only to the Kit's question keys (or null)", kit_question_key=qkey
            )
        seqs = [int(s) for s in item.get("segment_seqs") or []]
        if not seqs:
            raise common.invalid(
                "invalid_evidence", "Each response needs the transcript segments it came from"
            )
        seg = validators.check_any_quote(segs, seqs, str(item["response_quote"]))
        confidence = item.get("match_confidence")
        qa_rows.append(
            {
                "asked_text": str(item["asked_text"])[:500], "kit_question_key": qkey,
                "kit_question_text": questions[qkey]["text"] if qkey else None,
                "match_confidence": float(confidence) if confidence is not None else None,
                "response_quote": str(item["response_quote"])[:800], "segment_seqs": seqs[:20],
                "start_ms": seg.start_ms,
            }
        )  # fmt: skip
    await db.execute(
        update(InterviewCaptureOutput)
        .where(InterviewCaptureOutput.capture_id == capture.id, InterviewCaptureOutput.status == "ready")
        .values(status="superseded")
    )
    row = InterviewCaptureOutput(
        id=uuid.uuid4(), tenant_id=capture.tenant_id, capture_id=capture.id, interview_id=capture.interview_id,
        candidate_id=capture.candidate_id, transcript_id=capture.transcript_id, summary_by_competency=summary_rows,
        qa_items=qa_rows, overall_summary=(overall_summary or None) and overall_summary[:2000], status="ready",
        run_id=run_id, ai_generated=True,
    )  # fmt: skip
    db.add(row)
    capture.status = "ready"
    if run_id is not None:
        capture.agent_run_id = run_id
    await db.flush()
    audit.record(
        "interview_capture.outputs_saved",
        "interview",
        capture.interview_id,
        session=db,
        output_id=str(row.id),
    )
    return row


async def current(db: AsyncSession, capture_id: uuid.UUID) -> InterviewCaptureOutput | None:
    return (
        await db.execute(
            select(InterviewCaptureOutput).where(
                InterviewCaptureOutput.capture_id == capture_id, InterviewCaptureOutput.status == "ready"
            )
        )
    ).scalar_one_or_none()


__all__ = ["MAX_QA", "current", "save"]
