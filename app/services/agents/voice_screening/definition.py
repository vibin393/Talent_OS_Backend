# ruff: file-ignore[line-too-long]
"""The ``voice_screening`` AgentSpec (Epic 58, spec A6; FR-293..FR-296).

Runs are short and stage-driven: ``requested`` (the recruiter asked for a voice screen -> send the templated consent
link), ``consented`` (the Candidate agreed to the call -> schedule it; Manual hands the call to the recruiter) and
``session_ready`` (``VoiceSessionReady`` -> extract answers from the Candidate's transcript lines, save a factual
summary). Owner = the Application owner. Only text is processed: no tool accepts audio.
★ never: the screening outcome, stage moves, reject -- no tool in the allow-list reaches them.
"""

import uuid
from typing import Any

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.schemas.voice import VoiceResult
from app.services import voice_service
from app.services.agents.voice_screening.kickoff import kickoff

KEY = "voice_screening"
T = Scope.TENANT

VOICE_TOOLS: tuple[str, ...] = (
    "voice.session_get",
    "recording_policy.evaluate",
    "transcription.get",
    "voice.request_consent",
    "voice.schedule_call",
    "voice.transcribe",
    "voice.extract_answers",
    "voice.save_summary",
    "voice.propose_candidate_updates",
)
CORE_TOOLS: tuple[str, ...] = ("consent.status", "tasks.create", "notes.add")
TOOLS = VOICE_TOOLS + CORE_TOOLS

PERMISSION_CEILING = dict.fromkeys(
    (
        "application:read", "candidate:read", "screening:conduct", "voice:upload", "message:send_templated",
        "task:create", "note:write", "candidate:update",
    ),
    T,
)  # fmt: skip


def _ready_subject(event: Any) -> tuple[str, uuid.UUID] | None:
    return ("application", event.application_id) if getattr(event, "application_id", None) else None


def _ready_dedup(event: Any) -> str:
    return f"voice:ready:{event.voice_session_id}"


TRIGGERS = (
    TriggerSpec("requested", "manual"),
    TriggerSpec("consented", "manual"),
    TriggerSpec(
        "session_ready", "event", event="VoiceSessionReady", subject=_ready_subject, dedup_key=_ready_dedup
    ),
)


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    return f"/applications/{subject[1]}?panel=voice" if subject is not None else "/applications"


SPEC = AgentSpec(
    key=KEY,
    version=1,
    owner_epic="58",
    name="Voice Screening Agent",
    purpose="Collects consent for screening calls and turns call transcripts into answers for your review.",
    prompt_id="agent_voice_screening",
    model_tier="standard",
    effort="low",
    tools=TOOLS,
    permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(max_wait_hours=0),
    subject_type="application",
    origin="agent",
    triggers=TRIGGERS,
    owner=voice_service.application_owner,
    kickoff=kickoff,
    result_model=VoiceResult,
    fallback="owner_task",
    fallback_link=fallback_link,
    channels=("email", "sms", "whatsapp"),
    consent_purposes=("ai_processing", "voice_screening"),
    aedt_feature="agent_voice_screening",
    eval_set="evals/voice_screening",
    eval_threshold=0.9,
)  # fmt: skip

__all__ = ["CORE_TOOLS", "KEY", "PERMISSION_CEILING", "SPEC", "TOOLS", "VOICE_TOOLS"]
