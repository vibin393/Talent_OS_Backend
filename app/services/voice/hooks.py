# ruff: file-ignore[line-too-long]
"""Voice Screening wiring (Epic 58), autodiscovered by ``core.hooks``.

* the ``voice_screening`` and ``call_recording`` default consent notices;
* the ``files_service`` access checker for ``voice_session`` recordings (playback: ``screening:conduct`` + Application
  read, only while a recording exists);
* the ``voice`` connector webhook handler (Fake call events);
* event handlers: ``ConsentWithdrawn(call_recording | voice_screening)`` -> ``voice.purge_recordings`` (immediately);
  ``AgentRunFailed`` / ``AgentRunFinished`` of ``voice_screening`` while still processing -> hand the transcript to the recruiter;
* ★ ``voice_session`` -> ``reviewed`` is human only;
* notification types ``voice_session_ready``, ``voice_consent_refused``, ``voice_upload_needed``;
* settings hub entries and audit labels. (The ``voice_sessions`` table is already bound to the foundation's
  ``recordings`` class; retention bindings are keyed by table, so ``summary`` / ``gaps`` have no second binding --
  ``withdrawal.expire_summaries`` is ready for Epic 65 to chain; recorded deviation.)
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe
from app.services.voice.common import AGENT_KEY, FILE_OWNER, PURPOSE_CALL, PURPOSE_RECORDING


def _notices() -> None:
    from app.services.privacy.consent import register_default_notice
    from app.services.voice.consent import (
        CALL_NOTICE_BODY,
        CALL_NOTICE_TITLE,
        RECORDING_NOTICE_BODY,
        RECORDING_NOTICE_TITLE,
    )

    register_default_notice(PURPOSE_CALL, CALL_NOTICE_TITLE, CALL_NOTICE_BODY)
    register_default_notice(PURPOSE_RECORDING, RECORDING_NOTICE_TITLE, RECORDING_NOTICE_BODY)


def _files() -> None:
    from app.services import files_service
    from app.services.voice.playback import can_access

    files_service.register_access_checker(FILE_OWNER, can_access, plural="voice-sessions")


def _webhook() -> None:
    from app.services.connectors.registry import register_webhook_handler, webhook_handler
    from app.services.voice.provider_jobs import on_webhook

    if webhook_handler("voice") is None:
        register_webhook_handler("voice", on_webhook)


def _human_only() -> None:
    from app.services.workflow.catalogue import register_human_only_transitions

    register_human_only_transitions("voice_session", ["reviewed"])


def _subscribe() -> None:
    from app.events.agents import AgentRunFailed, AgentRunFinished
    from app.events.privacy import ConsentWithdrawn

    def on_withdrawn(e: ConsentWithdrawn) -> None:
        from app.tasks import voice as tasks
        from app.tasks.runtime import enqueue

        if e.purpose in (PURPOSE_CALL, PURPOSE_RECORDING):
            enqueue(tasks.purge_recordings, str(e.tenant_id), str(e.candidate_id), e.purpose)

    def on_failed(e: Any) -> None:
        from app.tasks import voice as tasks
        from app.tasks.runtime import enqueue

        if e.agent_key == AGENT_KEY and e.subject_type == "application" and e.subject_id is not None:
            enqueue(tasks.on_run_failed, str(e.tenant_id), str(e.run_id))

    def on_fake_call(e: Any) -> None:
        from app.tasks import voice as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.run_fake_call, str(e.tenant_id), str(e.voice_session_id))

    from app.services.voice.common import FakeCallScheduled

    on_fake_call.__name__ = "voice_on_fake_call_scheduled"
    subscribe(FakeCallScheduled, on_fake_call)
    on_withdrawn.__name__ = "voice_on_consent_withdrawn"
    on_failed.__name__ = "voice_on_run_failed"
    subscribe(ConsentWithdrawn, on_withdrawn)
    subscribe(AgentRunFailed, on_failed)
    subscribe(AgentRunFinished, on_failed)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def _register_notifications() -> None:
    from app.services import notifications_service as ns

    async def to_user(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]  # NOSONAR
        uid = _uuid(payload.get("user_id"))
        return {uid} if uid else set()

    def _link(payload: Any) -> tuple[uuid.UUID | None, str | None]:
        aid = _uuid(payload.get("application_id"))
        return aid, (f"/applications/{aid}?panel=voice" if aid else None)

    async def ready(db: AsyncSession, payload: Any) -> Any:
        from app.services import entity_refs

        cid = _uuid(payload.get("candidate_id"))
        name = await entity_refs.entity_label(db, "candidate", cid) if cid else None
        aid, link = _link(payload)
        return ns.Content(
            title=f"Voice screen for {name or 'a candidate'} is ready for review",
            body="AI-extracted answers from the call transcript are waiting for your review.",
            entity_type="application", entity_id=aid, link_path=link,
        )  # fmt: skip

    async def refused(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        aid, link = _link(payload)
        return ns.Content(
            title="The candidate declined the screening call", body="Screen them another way.",
            entity_type="application", entity_id=aid, link_path=link,
        )  # fmt: skip

    async def upload_needed(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        aid, link = _link(payload)
        return ns.Content(
            title="Upload the call transcript",
            body="A recording was uploaded without a transcript. Talent OS does not transcribe audio.",
            entity_type="application", entity_id=aid, link_path=link,
        )  # fmt: skip

    for key, label, channel, render in (
        ("voice_session_ready", "Voice screen ready for review", "both", ready),
        ("voice_consent_refused", "Candidate declined a screening call", "in_app", refused),
        ("voice_upload_needed", "Voice screen transcript needed", "in_app", upload_needed),
    ):
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(
                key,
                ns.NotificationType(label, "AI agents", channel, False, to_user, render, internal_only=True),
            )


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting", "voice_screening", "Voice screening", "Call method, consent links and upload limits for voice screens",
        "/api/v1/settings/agents/voice-screening", "ai:agent_configure", owner_epic="Epic 58",
    )  # fmt: skip
    register_settings_entry(
        "recruiting", "recording_policy", "Recording policy", "Where calls and interviews may be recorded, by jurisdiction",
        "/api/v1/recording-policies", "ai:agent_configure", owner_epic="Epic 58",
    )  # fmt: skip


def _register_audit() -> None:
    from app.core import audit

    for key, text in (
        ("voice_session.requested", "A voice screen was requested"),
        ("voice_session.consent_requested", "A voice screening consent request was sent"),
        ("voice_session.consent_recorded", "The candidate answered the voice screening consent request"),
        ("voice_session.manual_call", "A voice screening call was handed to the recruiter"),
        ("voice_session.scheduled", "A voice screening call was scheduled"),
        ("voice_session.transcript_uploaded", "A voice screening transcript was uploaded"),
        ("voice_session.recording_uploaded", "A voice screening recording was uploaded"),
        ("voice_session.speakers_labelled", "The candidate's lines in a voice transcript were marked"),
        ("voice_session.answers_extracted", "Answers were extracted from a voice transcript"),
        ("voice_session.reviewed", "A voice screen was reviewed"),
        ("voice_session.cancelled", "A voice screen was cancelled"),
        ("voice_session.recording_deleted", "A voice screening recording was deleted"),
        (
            "voice_session.withdrawn",
            "A voice screen's transcript and answers were deleted after a withdrawal",
        ),
        ("recording_policy.changed", "A recording policy rule changed"),
        ("recording_policy.deleted", "A recording policy rule was deleted"),
        ("voice_settings.changed", "The voice screening settings changed"),
    ):
        audit.register_audit_action(key, text)


for _fn in (
    _notices,
    _files,
    _webhook,
    _human_only,
    _subscribe,
    _register_notifications,
    _register_settings,
    _register_audit,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("voice hooks: {} failed", _fn.__name__)
        raise
