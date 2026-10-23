# ruff: file-ignore[line-too-long]
"""Interview Assistant facade (Epic 60; AD-1 naming). Entry point of the ``interview_captures`` domain for the routers,
tasks, the agent definition (``capture_owner``) and the Epic 60 agent tools (``agent_tools.{interviews,interview_capture,
skills}/tools_e60.py``)."""

from app.services.interview_captures import consent, drafts, uploads, visibility, withdrawal
from app.services.interview_captures.common import AGENT_KEY, FEATURE, PURPOSE, set_clock
from app.services.interview_captures.consent import (
    candidate_consent_decide,
    candidate_consent_view,
    capture_status,
    external_ack,
    external_ack_view,
    give_ack,
    my_ack,
    portal_decide,
    portal_view,
    request_acks,
    request_capture,
    revoke_ack,
)
from app.services.interview_captures.drafts import dismiss as dismiss_draft
from app.services.interview_captures.drafts import mark_opened as mark_draft_opened
from app.services.interview_captures.drafts import mine as my_draft
from app.services.interview_captures.drafts import on_scorecard_submitted
from app.services.interview_captures.service import (
    agent_capture,
    agent_create_draft,
    agent_interview,
    agent_kit,
    agent_policy_status,
    agent_save_summary,
    agent_suggest_evidence,
    agent_transcribe,
    agent_transcript,
    capture_owner,
    get_settings,
    kickoff_for,
    link_run,
    rerun,
    update_settings,
)
from app.services.interview_captures.uploads import (
    relabel,
    set_transcription_provider,
    upload_recording,
    upload_transcript,
)
from app.services.interview_captures.visibility import get_outputs

__all__ = [
    "AGENT_KEY",
    "FEATURE",
    "PURPOSE",
    "agent_capture",
    "agent_create_draft",
    "agent_interview",
    "agent_kit",
    "agent_policy_status",
    "agent_save_summary",
    "agent_suggest_evidence",
    "agent_transcribe",
    "agent_transcript",
    "candidate_consent_decide",
    "candidate_consent_view",
    "capture_owner",
    "capture_status",
    "consent",
    "dismiss_draft",
    "drafts",
    "external_ack",
    "external_ack_view",
    "get_outputs",
    "get_settings",
    "give_ack",
    "kickoff_for",
    "link_run",
    "mark_draft_opened",
    "my_ack",
    "my_draft",
    "on_scorecard_submitted",
    "portal_decide",
    "portal_view",
    "relabel",
    "request_acks",
    "request_capture",
    "rerun",
    "revoke_ack",
    "set_clock",
    "set_transcription_provider",
    "update_settings",
    "upload_recording",
    "upload_transcript",
    "uploads",
    "visibility",
    "withdrawal",
]
