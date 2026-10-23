# ruff: file-ignore[line-too-long]
"""The ``scheduling`` AgentSpec (Epic 59, spec A4; FR-297..FR-300).

A negotiation is a chain of short runs (coordinator C-12): ``requested`` (manual / workflow) computes and offers slots and
waits on ``("slot_pick", offer_id)``; ``slot_picked`` (inbound, ``on_active="attach"``) books; ``counter_proposed``
(inbound) checks the Candidate's times; ``reschedule_requested`` (manual) re-offers and moves the Interview;
``reminders`` / ``scorecard_nudges`` (schedule; the domain beat starts them -- their cron never fires) send templated
reminders. Owner = the Application owner (reminder runs: the interview organiser). ``fallback="none"``: a stopped wait
(900 s wall clock) is normal; a failed run hands the request to the recruiter (``AgentRunFailed`` handler).
★ never: whether a round happens, interview outcomes, no-show stage moves, Offers -- no tool reaches them.
"""

import uuid

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.schemas.scheduling_agent import SchedulingResult
from app.services import scheduling_agent_service
from app.services.agents.scheduling.kickoff import kickoff

KEY = "scheduling"
T = Scope.TENANT
NEVER = (
    "0 0 31 2 *"  # 31 February: the runtime scheduler never fires it; the scheduling beat starts these runs
)

SCHEDULING_TOOLS: tuple[str, ...] = (
    "interviews.plan",
    "interviews.participants",
    "calendar.free_busy",
    "holidays.for_location",
    "candidates.timezone",
    "scheduling.propose",
    "scheduling.check_times",
    "scheduling.offer_slots",
    "scheduling.offer_partial",
    "interviews.book",
    "interviews.reschedule",
    "interviews.cancel",
    "interviews.change_participants",
    "reminders.send",
)
CORE_TOOLS: tuple[str, ...] = (
    "tasks.create",
    "notes.add",
    "notify.user",
    "messaging.create_draft",
    "consent.status",
)
TOOLS = SCHEDULING_TOOLS + CORE_TOOLS

PERMISSION_CEILING = dict.fromkeys(
    (
        "interview:read", "interview:schedule", "calendar:free_busy", "application:read", "candidate:read",
        "message:send_templated", "message:draft", "task:create", "note:write",
    ),
    T,
)  # fmt: skip

TRIGGERS = (
    TriggerSpec("requested", "manual"),
    TriggerSpec("slot_picked", "inbound", on_active="attach"),
    TriggerSpec("counter_proposed", "inbound", on_active="attach"),
    TriggerSpec("reschedule_requested", "manual"),
    TriggerSpec("reminders", "schedule", schedule=NEVER),
    TriggerSpec("scorecard_nudges", "schedule", schedule=NEVER),
)


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    return f"/applications/{subject[1]}?tab=interviews" if subject is not None else "/interviews"


SPEC = AgentSpec(
    key=KEY,
    version=1,
    owner_epic="59",
    name="Scheduling Agent",
    purpose="Finds interview times from calendars, lets candidates pick, books, reschedules and reminds.",
    prompt_id="agent_scheduling",
    model_tier="standard",
    effort="low",
    tools=TOOLS,
    permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(max_wait_hours=168),
    subject_type="application",
    origin="agent",
    triggers=TRIGGERS,
    owner=scheduling_agent_service.application_owner,
    kickoff=kickoff,
    result_model=SchedulingResult,
    fallback="none",
    fallback_link=fallback_link,
    channels=("email", "whatsapp", "sms", "portal"),
    consent_purposes=("ai_processing",),
    eval_set="evals/scheduling",
    eval_threshold=0.9,
)  # fmt: skip

__all__ = ["CORE_TOOLS", "KEY", "PERMISSION_CEILING", "SCHEDULING_TOOLS", "SPEC", "TOOLS"]
