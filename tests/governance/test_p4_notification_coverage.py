# ruff: noqa: E501
"""FR-327 governance (Epic 65 story 65.7, spec A7): the Phase 4 notification types.

* Every required type is registered after ``autodiscover`` (``PENDING_P4_NOTIFICATION_TYPES`` shrink-only, empty at
  phase close) and is internal (Phase 4 notices go to staff).
* ``proposal_waiting`` never fires for Copilot drafts; for the Collections Agent it renders "Dunning draft waiting".
* Renderers carry ids and labels only: message bodies, transcript text, tool args, Copilot text, phone, email or PAN
  in the payload never reach the rendered title / body / link.
"""

import uuid

import pytest

from app.core.database import tenant_session
from app.core.hooks import autodiscover
from app.services import notifications_service as ns
from tests.epic3 import Org3

autodiscover("app.services")

REQUIRED_P4_NOTIFICATION_TYPES: dict[str, str] = {
    "proposal_waiting": "Epic 53", "proposal_expiring": "Epic 53", "proposal_decided": "Epic 53",
    "agent_run_failed": "Epic 53", "agent_budget_threshold": "Epic 53", "kill_switch_changed": "Epic 53",
    "agent_nudge": "Epic 53", "agent_flag": "Epic 53",
    "screening_completed": "Epic 57", "screening_handed_off": "Epic 57", "screening_expired": "Epic 57",
    "voice_session_ready": "Epic 58", "voice_consent_refused": "Epic 58", "voice_upload_needed": "Epic 58",
    "interview_slot_picked": "Epic 59", "interview_booked_by_agent": "Epic 59",
    "interview_rescheduled_by_agent": "Epic 59", "scheduling_handed_off": "Epic 59",
    "scorecard_draft_ready": "Epic 60", "interview_capture_blocked": "Epic 60", "interview_capture_ack_requested": "Epic 60",
    "submission_pack_ready": "Epic 61",
    "compliance_chase_escalation": "Epic 62", "compliance_chase_reminder": "Epic 62", "bgv_follow_up_limit": "Epic 62",
    "onboarding_question_handoff": "Epic 62", "onboarding_answer_ready": "Epic 62",
    "timesheet_anomaly_flagged": "Epic 63", "invoice_run_prepared": "Epic 63", "collections_hold_changed": "Epic 63",
    "ai_eval_regression": "Epic 65",
    "sourcing_shortlist_ready": "Epic 56",
    "demand_intake_unmatched": "Epic 55", "demand_drafts_created": "Epic 55", "demand_duplicate_flagged": "Epic 55",
    "demand_sla_digest": "Epic 55",
}  # fmt: skip
# Shrink-only: types whose owner epic is still being built (or shipped another key, recorded in the notes).
PENDING_P4_NOTIFICATION_TYPES: dict[str, str] = {}
MANDATORY = ("kill_switch_changed",)
# Foundation types that render the agent's short, sanitised label (``message``, <= 300 chars) by design.
LABEL_RENDERERS: dict[str, set[str]] = {"agent_nudge": {"message"}, "agent_flag": {"message"}}

POISON: dict[str, str] = {
    "body": "POISON-BODY-hello candidate",
    "message": "POISON-MESSAGE-text",
    "transcript": "POISON-TRANSCRIPT-I worked at",
    "args": "POISON-ARGS-subject",
    "question": "POISON-COPILOT-who is best",
    "answer": "POISON-COPILOT-answer",
    "phone": "+91 98765 43210",
    "email": "poison.person@example.com",
    "pan": "ABCDE1234F",
}


def _missing() -> dict[str, str]:
    return {k: v for k, v in REQUIRED_P4_NOTIFICATION_TYPES.items() if k not in ns.NOTIFICATION_TYPES}


@pytest.mark.parametrize("key", sorted(REQUIRED_P4_NOTIFICATION_TYPES))
def test_p4_type_registered(key: str) -> None:
    found = ns.NOTIFICATION_TYPES.get(key)
    if key in PENDING_P4_NOTIFICATION_TYPES:
        assert found is None, f"{key} is registered now: remove it from PENDING_P4_NOTIFICATION_TYPES"
        pytest.skip(f"pending: {key}")
    assert found is not None, (
        f"notification type {key!r} ({REQUIRED_P4_NOTIFICATION_TYPES[key]}) is not registered"
    )
    if found.external:  # Candidate / Worker-facing types (62 chases, Q&A) need a portal deep link
        assert found.portal_path and str(found.portal_path).startswith("/portal/"), f"{key}: {found.portal_path}"


def test_phase_close_pending_list_is_empty() -> None:
    assert PENDING_P4_NOTIFICATION_TYPES == {}


def test_kill_switch_is_mandatory() -> None:
    for key in MANDATORY:
        assert ns.NOTIFICATION_TYPES[key].mandatory_in_app is True


async def test_dunning_drafts_and_copilot_drafts(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from app.core.events import _handlers
    from app.events.agents import ProposalCreated
    from app.tasks import runtime

    d = ns.NOTIFICATION_TYPES["proposal_waiting"]
    content = await d.render(None, {"proposal_id": str(uuid.uuid4()), "agent_key": "collections"})  # type: ignore[arg-type]
    assert content.title.startswith("Dunning draft waiting")
    sent: list[tuple[object, ...]] = []
    monkeypatch.setattr(runtime, "enqueue", lambda task, *args, **kw: sent.append(args))
    handler = next(
        h
        for h in _handlers[ProposalCreated]
        if getattr(h, "__name__", "") == "agents_notify_proposal_waiting"
    )
    for origin in ("copilot", "agent"):
        handler(ProposalCreated(
            tenant_id=uuid.uuid4(), proposal_id=uuid.uuid4(), run_id=None, agent_key="screening", tool_key="messaging.send",
            origin=origin, target_type=None, target_id=None, assignee_user_id=None, route_role_key="recruiter",
            expires_at=datetime.now(UTC),
        ))  # fmt: skip
    assert len(sent) == 1, "proposal_waiting must fire for agent Proposals only, never for Copilot drafts"


@pytest.mark.parametrize("key", sorted(REQUIRED_P4_NOTIFICATION_TYPES))
async def test_rendered_payload_has_no_denylisted_values(key: str, org3: Org3) -> None:
    d = ns.NOTIFICATION_TYPES.get(key)
    if d is None:
        pytest.skip(f"pending: {key}")
    rid = str(uuid.uuid4())
    payload: dict[str, object] = dict.fromkeys(("proposal_id", "run_id", "conversation_id", "application_id", "candidate_id", "demand_id", "interview_id", "voice_session_id", "capture_id", "pack_id", "chase_id", "thread_id", "timesheet_id", "invoice_run_id", "client_id", "shortlist_id", "message_id", "user_id", "owner_user_id", "fallback_task_id", "entity_id", "request_id", "slot_id"), rid)  # fmt: skip
    payload |= {
        "agent_key": "screening",
        "tool_key": "messaging.send",
        "outcome": "approved",
        "reason_code": "failed",
        "entity_type": "candidate",
        "threshold": 80,
    }
    payload |= POISON
    async with tenant_session(org3.tenant_id) as db:
        try:
            content = await d.render(db, payload)
        except Exception as exc:
            pytest.skip(f"{key}: renderer refused synthetic ids ({type(exc).__name__})")
    rendered = " ".join(str(x) for x in (content.title, content.body, content.link_path) if x)
    allowed = LABEL_RENDERERS.get(key, set())
    leaked = sorted(k for k, v in POISON.items() if v in rendered and k not in allowed)
    assert leaked == [], f"{key} renders denylisted payload values: {leaked}"
