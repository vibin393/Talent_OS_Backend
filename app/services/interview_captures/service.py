# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Interview Assistant service (Epic 60): settings, manual re-runs, the agent's Accountable Owner and the operations
behind the agent tools (``agent_ops``-style functions take the tool context's request + db and the run subject).

Agent operations re-check that the capture belongs to the run's Interview (another interview's capture -> 404), that
the capture is still consented (``interview_recording`` + acknowledgements) and never write P1 scorecards.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.agentic import Kickoff
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.interview_captures import InterviewCapture
from app.schemas.interview_captures import AssistantSettingsIn, AssistantSettingsOut, RerunOut
from app.services.interview_captures import common, consent, drafts, outputs, skill_evidence, uploads


# ================================================================ settings
def _settings_out(row: Any) -> AssistantSettingsOut:
    return AssistantSettingsOut(
        show_suggestions_by_default=row.show_suggestions_by_default, allow_recording_upload=row.allow_recording_upload,
        allow_transcript_upload=row.allow_transcript_upload, consent_link_days=row.consent_link_days,
        max_upload_mb=row.max_upload_mb,
    )  # fmt: skip


async def get_settings(ctx: RequestContext, db: AsyncSession) -> AssistantSettingsOut:  # ruff: ignore[unused-function-argument]
    return _settings_out(await common.settings_row(db))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: AssistantSettingsIn
) -> AssistantSettingsOut:
    require_human(actor_from_ctx(ctx))
    row = await common.settings_row(db)
    changes = data.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(row, key, value)
    await db.flush()
    audit.record(
        "interview_assistant.settings_changed", "tenant", row.tenant_id, session=db, fields=sorted(changes)
    )
    return _settings_out(row)


# ================================================================ runs
async def capture_owner(
    ctx: Any, db: AsyncSession, subject: tuple[str, uuid.UUID] | None
) -> uuid.UUID | None:
    """Accountable Owner of an assistant run: the uploader (else the requester) of the Interview's capture."""
    if subject is None or subject[0] != "interview":
        return None
    capture = await common.capture_for_interview(db, subject[1])
    if capture is None:
        return None
    return capture.uploaded_by or capture.requested_by


def kickoff_for(capture: InterviewCapture, interview: Any) -> Kickoff:
    kit = common.kit_of(interview)
    from app.services.interview_captures.common import (
        interviewers,  # ruff: ignore[unused-import] - documented below
    )

    return Kickoff(
        inputs={
            "capture_id": str(capture.id), "interview_id": str(interview.id),
            "competency_keys": ",".join(kit.competency_keys), "question_keys": ",".join(kit.question_keys) or "none",
            "rating_scale": str(kit.rating_scale),
            "next": "read interviews.get (interviewer participant ids), interview_kits.get and interview_capture.transcript",
        },
        candidate_ids=(capture.candidate_id,),
    )  # fmt: skip


async def rerun(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> RerunOut:
    from app.services import agent_runtime_service

    require_human(actor_from_ctx(ctx))
    interview = await consent.manageable_interview(ctx, db, interview_id)
    capture = await common.capture_for_interview(db, interview_id, for_update=True)
    if (
        capture is None
        or capture.transcript_id is None
        or capture.status not in ("processing", "ready", "uploaded")
    ):
        raise common.conflict("no_transcript", "Upload the meeting transcript first")
    if capture.speakers:
        raise common.conflict("speakers_unknown", "Tell us who the candidate is first")
    ev = await consent.refresh(db, interview, capture)
    if not ev.permitted:
        raise common.conflict(
            "capture_not_permitted", "Not everyone has agreed to recording", reasons=ev.reasons
        )
    ref = await agent_runtime_service.start_run(
        ctx, db, agent_key=common.AGENT_KEY, trigger_kind="manual", trigger_key="manual",
        subject=("interview", interview_id), kickoff=kickoff_for(capture, interview),
        owner_user_id=common.principal_of(ctx).id, dedup_key=f"ia:capture:{capture.id}",
    )  # fmt: skip
    if ref.run_id is not None:
        capture.agent_run_id = ref.run_id
        if capture.status == "uploaded":
            capture.status = "processing"
        await db.flush()
    return RerunOut(run_id=ref.run_id, status=ref.status, reason=ref.reason)


async def link_run(db: AsyncSession, interview_id: uuid.UUID, run_id: uuid.UUID) -> None:
    capture = await common.capture_for_interview(db, interview_id, for_update=True)
    if capture is not None and capture.status != "erased":
        capture.agent_run_id = run_id
        await db.flush()


# ================================================================ agent operations (tool handlers)
async def agent_capture(
    db: AsyncSession, subject: tuple[str, uuid.UUID] | None, capture_id: uuid.UUID, *, lock: bool = False
) -> InterviewCapture:
    """The run's capture: it must belong to the run's Interview and still be consented (else 404 / 409)."""
    try:
        capture = await common.capture_row(db, capture_id, for_update=lock)
    except NotFound:
        raise NotFound("Interview capture not found") from None
    if subject is None or subject[0] != "interview" or capture.interview_id != subject[1]:
        raise NotFound("Interview capture not found")
    if capture.status == "erased":
        raise common.conflict("capture_erased", "The capture was erased after a withdrawal")
    interview = await common.interview_row(db, capture.interview_id)
    ev = await consent.evaluate(db, interview, capture)
    if any(r == "missing_consent:candidate" or r.startswith("missing_ack:") for r in ev.reasons):
        raise common.conflict("consent_missing", "Not every participant has agreed to recording")
    return capture


async def agent_interview(
    db: AsyncSession, subject: tuple[str, uuid.UUID] | None, interview_id: uuid.UUID
) -> dict[str, Any]:
    from app.services import entity_refs

    if subject is None or subject[0] != "interview" or subject[1] != interview_id:
        raise NotFound("Interview not found")
    interview = await common.interview_row(db, interview_id)
    capture = await common.capture_for_interview(db, interview_id)
    parts = await common.participants(db, interview_id)
    internal, _ = common.interviewers(parts)
    users = await entity_refs.users(db, [p.user_id for p in internal if p.user_id])
    return {
        "interview": interview,
        "capture": capture,
        "interviewers": [
            {
                "participant_id": p.id, "user_id": p.user_id,
                "name": p.name or (users[p.user_id].name if p.user_id in users else None),
                "scorecard_required": bool(p.scorecard_required and p.kind == "interviewer_internal"),
            }
            for p in internal
        ],
    }  # fmt: skip


async def agent_kit(
    db: AsyncSession, subject: tuple[str, uuid.UUID] | None, interview_id: uuid.UUID
) -> common.KitView:
    if subject is None or subject[0] != "interview" or subject[1] != interview_id:
        raise NotFound("Interview not found")
    return common.kit_of(await common.interview_row(db, interview_id))


async def agent_policy_status(
    db: AsyncSession, subject: tuple[str, uuid.UUID] | None, capture_id: uuid.UUID
) -> dict[str, Any]:
    capture = await common.capture_row(db, capture_id)
    if subject is None or subject[0] != "interview" or capture.interview_id != subject[1]:
        raise NotFound("Interview capture not found")
    interview = await common.interview_row(db, capture.interview_id)
    ev = await consent.evaluate(db, interview, capture)
    return {
        "permitted": ev.permitted and capture.status != "erased",
        "reasons": list(ev.reasons),
        "recording_rule": ev.rule,
    }


async def agent_transcript(
    ctx: RequestContext, db: AsyncSession, subject: Any, capture_id: uuid.UUID, max_chars: int
) -> str:
    from app.services import transcripts_service

    capture = await agent_capture(db, subject, capture_id)
    if capture.transcript_id is None:
        raise common.conflict(
            "no_transcript", "There is no transcript yet; ask the uploader for the meeting transcript"
        )
    if capture.speakers:
        raise common.conflict("speakers_unknown", "The uploader must say who the candidate is first")
    from app.services.transcripts.parsers import format_ts

    out = await transcripts_service.get(
        uploads.reader_ctx(ctx), db, capture.transcript_id, include_segments=True
    )
    if out.status != "ready":
        raise common.conflict("no_transcript", "The transcript is not available")
    lines: list[str] = []
    used = 0
    for seg in out.segments:
        line = f"[#{seg.seq} {format_ts(seg.start_ms)}] {seg.speaker_role.upper()}: {seg.text}"
        if used + len(line) + 1 > max_chars:
            lines.append("[transcript truncated]")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


async def agent_save_summary(
    db: AsyncSession, subject: Any, run_id: uuid.UUID | None, capture_id: uuid.UUID, data: dict[str, Any]
) -> uuid.UUID:
    capture = await agent_capture(db, subject, capture_id, lock=True)
    row = await outputs.save(
        db, capture, summary_by_competency=data["summary_by_competency"], qa_items=data["qa_items"],
        overall_summary=data.get("overall_summary"), run_id=run_id,
    )  # fmt: skip
    return row.id


async def agent_create_draft(
    db: AsyncSession,
    subject: Any,
    run_id: uuid.UUID | None,
    capture_id: uuid.UUID,
    participant_id: uuid.UUID,
    ratings: list[dict[str, Any]],
    answers: list[dict[str, Any]],
) -> tuple[uuid.UUID, uuid.UUID]:
    capture = await agent_capture(db, subject, capture_id, lock=True)
    draft = await drafts.save(
        db, capture, participant_id=participant_id, ratings=ratings, answers=answers, run_id=run_id
    )
    return draft.id, capture.candidate_id


async def agent_suggest_evidence(
    ctx: RequestContext, db: AsyncSession, subject: Any, capture_id: uuid.UUID, items: list[dict[str, Any]]
) -> tuple[int, list[dict[str, str]], uuid.UUID]:
    capture = await agent_capture(db, subject, capture_id)
    suggested, skipped = await skill_evidence.suggest_from_capture(ctx, db, capture, items)
    return suggested, skipped, capture.candidate_id


async def agent_transcribe(
    ctx: RequestContext, db: AsyncSession, subject: Any, capture_id: uuid.UUID
) -> uuid.UUID:
    capture = await agent_capture(db, subject, capture_id, lock=True)
    if uploads.transcription_provider() is None:
        raise common.conflict(
            "not_available", "Speech-to-text isn't available; ask the uploader for the meeting transcript"
        )
    return await uploads.transcribe(ctx, db, capture)


__all__ = [
    "agent_capture",
    "agent_create_draft",
    "agent_interview",
    "agent_kit",
    "agent_policy_status",
    "agent_save_summary",
    "agent_suggest_evidence",
    "agent_transcribe",
    "agent_transcript",
    "capture_owner",
    "get_settings",
    "kickoff_for",
    "link_run",
    "rerun",
    "update_settings",
]
