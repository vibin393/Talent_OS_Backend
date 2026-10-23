# ruff: file-ignore[line-too-long]
"""Interview Assistant wiring (Epic 60), autodiscovered by ``core.hooks``.

* the ``interview_recording`` default consent notice;
* the P3 evidence verifier for skill suggestions of source ``interview`` (C-10): verified only when the accepting User
  is an internal interviewer on that Interview;
* event handlers (after commit -> ``interview_captures.*`` tasks): ``ConsentWithdrawn(interview_recording)`` ->
  withdrawal, ``ScorecardSubmitted`` -> suggestion decisions, ``InterviewCancelled`` -> cancel the assistant run,
  ``AgentRunFinished`` / ``AgentRunFailed`` of ``interview_assist`` -> the capture's failure state;
* notification types ``scorecard_draft_ready``, ``interview_capture_blocked``, ``interview_capture_ack_requested``;
* the settings hub entry, audit labels and the retention binding of ``interview_capture_outputs``
  (``conversation_records``: summaries and quotes are erased).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe
from app.services.interview_captures.common import AGENT_KEY, PURPOSE


def _notice() -> None:
    from app.services.interview_captures.consent import NOTICE_BODY, NOTICE_TITLE
    from app.services.privacy.consent import register_default_notice

    register_default_notice(PURPOSE, NOTICE_TITLE, NOTICE_BODY)


def _verifier() -> None:
    from app.services import skills_service
    from app.services.interview_captures.skill_evidence import SOURCE, verify_level

    skills_service.register_evidence_verifier(SOURCE, verify_level)


def _subscribe() -> None:
    from app.events.agents import AgentRunFailed, AgentRunFinished
    from app.events.interviews import InterviewCancelled, ScorecardSubmitted
    from app.events.privacy import ConsentWithdrawn

    def on_withdrawn(e: ConsentWithdrawn) -> None:
        from app.tasks import interview_captures as tasks
        from app.tasks.runtime import enqueue

        if e.purpose == PURPOSE:
            enqueue(tasks.withdraw_for_candidate, str(e.tenant_id), str(e.candidate_id))

    def on_submitted(e: ScorecardSubmitted) -> None:
        from app.tasks import interview_captures as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.on_scorecard_submitted, str(e.tenant_id), str(e.scorecard_id))

    def on_cancelled(e: InterviewCancelled) -> None:
        from app.tasks import interview_captures as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.on_interview_cancelled, str(e.tenant_id), str(e.interview_id))

    def on_run_end(e: Any) -> None:
        from app.tasks import interview_captures as tasks
        from app.tasks.runtime import enqueue

        if e.agent_key == AGENT_KEY and e.subject_type == "interview" and e.subject_id is not None:
            enqueue(
                tasks.on_run_ended,
                str(e.tenant_id),
                str(e.subject_id),
                str(e.run_id),
                str(getattr(e, "status", "failed")),
                e.reason_code,
            )

    for fn, name in (
        (on_withdrawn, "withdrawn"),
        (on_submitted, "scorecard_submitted"),
        (on_cancelled, "interview_cancelled"),
        (on_run_end, "run_ended"),
    ):
        fn.__name__ = f"interview_captures_on_{name}"
    subscribe(ConsentWithdrawn, on_withdrawn)
    subscribe(ScorecardSubmitted, on_submitted)
    subscribe(InterviewCancelled, on_cancelled)
    subscribe(AgentRunFinished, on_run_end)
    subscribe(AgentRunFailed, on_run_end)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def _register_notifications() -> None:
    from app.services import notifications_service as ns

    async def to_user(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        uid = _uuid(payload.get("user_id"))
        return {uid} if uid else set()

    async def draft_ready(db: AsyncSession, payload: Any) -> Any:
        from app.services import entity_refs

        cid = _uuid(payload.get("candidate_id"))
        name = await entity_refs.entity_label(db, "candidate", cid) if cid else None
        iid = _uuid(payload.get("interview_id"))
        return ns.Content(
            title=f"Your draft scorecard for {name or 'the candidate'} is ready",
            body="AI-generated suggestions for your review. Nothing is submitted until you submit your scorecard.",
            entity_type="interview", entity_id=iid, link_path=f"/interviews/{iid}/scorecard" if iid else None,
        )  # fmt: skip

    async def blocked(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        iid = _uuid(payload.get("interview_id"))
        return ns.Content(
            title="Recording isn't available for this interview",
            body="The recording policy doesn't allow it or the candidate's location is unknown.",
            entity_type="interview", entity_id=iid, link_path=f"/interviews/{iid}" if iid else None,
        )  # fmt: skip

    async def ack_requested(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        iid = _uuid(payload.get("interview_id"))
        return ns.Content(
            title="Agree to interview recording",
            body="An interview you take part in is waiting for your agreement to be recorded or transcribed.",
            entity_type="interview", entity_id=iid, link_path="/me/recording-consent",
        )  # fmt: skip

    for key, label, channel, render in (
        ("scorecard_draft_ready", "Draft scorecard ready", "both", draft_ready),
        ("interview_capture_blocked", "Interview recording not available", "in_app", blocked),
        (
            "interview_capture_ack_requested",
            "Interview recording agreement requested",
            "in_app",
            ack_requested,
        ),
    ):
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(
                key,
                ns.NotificationType(label, "AI agents", channel, False, to_user, render, internal_only=True),
            )


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting",
        "interview_assistant",
        "Interview Assistant",
        "Interview recording uploads, consent links and AI rating suggestions",
        "/api/v1/settings/agents/interview-assistant",
        "ai:agent_configure",
        owner_epic="Epic 60",
    )


def _register_audit() -> None:
    from app.core import audit

    for key, text in (
        ("interview_capture.requested", "Interview recording was requested"),
        ("interview_capture.consent_requested", "Interview recording consent was requested"),
        ("interview_capture.ack_given", "An interviewer agreed to interview recording"),
        ("interview_capture.ack_revoked", "An interviewer withdrew their interview recording agreement"),
        ("interview_capture.transcript_uploaded", "An interview transcript was uploaded"),
        ("interview_capture.recording_uploaded", "An interview recording was uploaded"),
        ("interview_capture.outputs_saved", "The Interview Assistant saved its summary"),
        ("interview_capture.draft_saved", "The Interview Assistant saved a draft scorecard"),
        ("interview_capture.draft_decided", "Draft scorecard suggestions were accepted, changed or ignored"),
        (
            "interview_capture.withdrawn",
            "An interview recording and its transcript were deleted after a withdrawal",
        ),
        ("interview_assistant.settings_changed", "The Interview Assistant settings changed"),
    ):
        audit.register_audit_action(key, text)


def _register_retention() -> None:
    from app.services.interview_captures import withdrawal
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    if "interview_capture_outputs" not in retention_bindings():
        register_retention_binding(
            "interview_capture_outputs",
            "conversation_records",
            subject_resolver=withdrawal.output_subject,
            anchor_resolver=withdrawal.output_anchor,
            expire_fn=withdrawal.expire_outputs,
        )


for _fn in (
    _notice,
    _verifier,
    _subscribe,
    _register_notifications,
    _register_settings,
    _register_audit,
    _register_retention,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("interview_captures hooks: {} failed", _fn.__name__)
        raise
