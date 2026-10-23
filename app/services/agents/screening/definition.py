# ruff: file-ignore[line-too-long]
"""The ``screening`` AgentSpec (Epic 57, spec A5; FR-289..FR-292).

A conversation is a chain of short runs (coordinator C-12): ``manual`` / ``stage_entry`` start it, ``reply`` (inbound,
``on_active="attach"``) continues it, ``reminder`` (domain beat; its cron never fires in the runtime scheduler) sends one
templated reminder. Owner = the Application owner. ``fallback="none"``: a stopped wait (900 s wall clock) is normal for a
multi-day conversation; real failures hand the conversation to the recruiter (``AgentRunFailed`` handler).
★ never: the screening outcome, stage moves, reject -- no tool in the allow-list reaches them.
"""

import uuid

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.schemas.screening_agent import ScreeningResult
from app.services import screening_agent_service
from app.services.agents.screening.kickoff import kickoff

KEY = "screening"
T = Scope.TENANT
NEVER = (
    "0 0 31 2 *"  # 31 February: the runtime scheduler never fires it; the screening beat starts reminder runs
)

SCREENING_TOOLS: tuple[str, ...] = (
    "screening.questionnaire",
    "postings.get",
    "candidates.get_contact_channels",
    "screening.invite",
    "screening.ask",
    "screening.clarify",
    "screening.reply_grounded",
    "screening.extract_answers",
    "screening.save_summary",
    "screening.reminder",
    "screening.handoff",
    "screening.propose_candidate_updates",
)
CORE_TOOLS: tuple[str, ...] = ("consent.status", "tasks.create", "notes.add", "flags.raise")
TOOLS = SCREENING_TOOLS + CORE_TOOLS

PERMISSION_CEILING = dict.fromkeys(
    (
        "application:read", "candidate:read", "demand:read", "screening:conduct", "message:send_templated",
        "messaging:send", "email:send", "task:create", "note:write", "candidate:update",
    ),
    T,
)  # fmt: skip

TRIGGERS = (
    TriggerSpec("manual", "manual"),
    TriggerSpec("stage_entry", "workflow"),
    TriggerSpec("reply", "inbound", on_active="attach"),
    TriggerSpec("reminder", "schedule", schedule=NEVER),
)


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    return f"/applications/{subject[1]}?panel=screening" if subject is not None else "/applications"


SPEC = AgentSpec(
    key=KEY,
    version=1,
    owner_epic="57",
    name="Screening Agent",
    purpose="Runs a pre-screening conversation from an approved questionnaire and prepares answers for your review.",
    prompt_id="agent_screening",
    model_tier="standard",
    effort="medium",
    tools=TOOLS,
    permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(max_wait_hours=168),
    subject_type="application",
    origin="conversation",
    triggers=TRIGGERS,
    owner=screening_agent_service.application_owner,
    kickoff=kickoff,
    result_model=ScreeningResult,
    fallback="none",
    fallback_link=fallback_link,
    channels=("portal", "email", "whatsapp"),
    consent_purposes=("ai_processing", "ai_agent_communication"),
    aedt_feature="agent_screening",
    eval_set="evals/screening",
    eval_threshold=0.9,
)  # fmt: skip

__all__ = ["CORE_TOOLS", "KEY", "PERMISSION_CEILING", "SCREENING_TOOLS", "SPEC", "TOOLS"]
