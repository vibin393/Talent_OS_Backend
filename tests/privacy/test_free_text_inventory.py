# ruff: noqa: E501
"""Epic 78 story 78.4 AC (FR-404, AD-90): the free-text inventory of every Phase 4 / Phase 5 table.

Every column of a P4/P5 model (``P4_P5_MODEL_MODULES``) that is ``Text``, ``String(>= 300)`` or ``JSONB`` and named like
free text, or listed in the model's ``__pii_fields__``, must be classified in ``FREE_TEXT_INVENTORY``:

* ``sealed`` -- encrypted under the Tenant DEK (``vault_service.seal_field``);
* ``payload_store`` -- kept in the P4 agent payload store (sealed with ``agent_payload``);
* ``justified_plaintext`` -- plaintext under RLS + retention, with the reason (known gaps are marked).

An unlisted column fails (an owner adding free text must classify it here). ``onboarding_qa_messages`` must hold sealed
text only: the plaintext columns no longer exist after ``20261010_512_03_qa_plain_drop``.
"""

import re
from typing import Literal

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.core.models import Base

Kind = Literal["sealed", "payload_store", "justified_plaintext"]

P4_P5_MODEL_MODULES = (
    # Phase 4 (Epics 53-65)
    "agent_runtime", "interview_captures", "recording_policy", "transcripts", "voice", "copilot", "analytics_answers",
    "screening_agent", "demand_insights", "demand_intake", "sourcing_agent", "scheduling_agent", "submission_packs",
    "chasing", "knowledge", "collection_rules", "timesheet_flags", "ai_evals", "proposal_signals",
    # Phase 5 (Epics 66-79)
    "ecosystem", "integrations", "metering", "oauth", "public_events", "vault", "public_api", "telephony", "webhooks",
    "marketplace", "campaign_copy", "mcp", "ecosystem_governance",
)  # fmt: skip
_NAME = re.compile(r"(text|body|note|comment|summary|content|answer|question|message|description|reason|details)")

_OPS = "operational text written by staff / the system (no candidate free text); RLS + retention"
_AGENT = "agent output about a record, shown to internal users; RLS + the agent-run retention"
FREE_TEXT_INVENTORY: dict[str, tuple[Kind, str]] = {
    # ---- sealed (Epic 78)
    "onboarding_qa_messages.text_sealed": ("sealed", "worker questions and answers under the Tenant DEK (AD-90)"),
    "onboarding_qa_messages.feedback_note_sealed": ("sealed", "the asker's feedback note under the Tenant DEK"),
    # ---- Phase 4
    "interview_capture_outputs.summary_by_competency": ("justified_plaintext", "AI interview notes for the panel; capture retention + withdrawal purge (Epic 60)"),
    "interview_capture_outputs.overall_summary": ("justified_plaintext", "AI interview notes for the panel; capture retention + withdrawal purge (Epic 60)"),
    "interview_scorecard_drafts.answers": ("justified_plaintext", "draft scorecard answers by the interviewer; scorecard retention"),
    "interview_captures.blocked_reasons": ("justified_plaintext", "reason codes only"),
    "transcript_segments.text": ("justified_plaintext", "KNOWN GAP: transcripts under RLS + recording retention; sealing planned in a later phase"),
    "voice_sessions.summary": ("justified_plaintext", "screening call summary for the recruiter; voice retention"),
    "recording_policies.note": ("justified_plaintext", _OPS),
    "copilot_conversations.context_ref": ("justified_plaintext", "ids of the page the user was on"),
    "copilot_conversations.context_summary": ("justified_plaintext", "short context label; 90-day Copilot retention"),
    "copilot_messages.content": ("justified_plaintext", "KNOWN GAP: the user's own Copilot chat, private to them; 90-day retention"),
    "copilot_feedback.comment": ("justified_plaintext", "the user's own rating comment (shown to AI admins, the UI says so)"),
    "analytics_answers.question": ("justified_plaintext", "the user's analytics question (no rows stored); answer retention"),
    "screening_questionnaire_versions.questions": ("justified_plaintext", "tenant questionnaire content, not personal data"),
    "screening_questionnaire_versions.change_note": ("justified_plaintext", _OPS),
    "screening_turns.body": ("justified_plaintext", "KNOWN GAP: screening chat turns under RLS + screening retention / erasure handler"),
    "screening_conversations.summary": ("justified_plaintext", _AGENT),
    "screening_answers.answer_text": ("justified_plaintext", "KNOWN GAP: structured screening answers under RLS + retention / erasure handler"),
    "demand_intake_sender_domains.note": ("justified_plaintext", _OPS),
    "demand_intake_messages.body_preview": ("justified_plaintext", "client requirement email preview; raw content purged after 180 days"),
    "demand_flags.reasons": ("justified_plaintext", "reason codes"),
    "demand_flags.summary": ("justified_plaintext", _AGENT),
    "demand_flags.resolution_note": ("justified_plaintext", _OPS),
    "agent_shortlists.summary": ("justified_plaintext", _AGENT),
    "agent_shortlist_entries.agent_note": ("justified_plaintext", _AGENT),
    "agent_shortlist_entries.draft_body": ("justified_plaintext", "templated outreach draft; shortlist retention"),
    "scheduling_requests.reschedule_reason": ("justified_plaintext", "reason code / short reason; scheduling retention"),
    "scheduling_slot_offers.candidate_note": ("justified_plaintext", "the candidate's short note on a slot pick; scheduling retention / erasure handler"),
    "submission_packs.summary_text": ("justified_plaintext", "candidate summary prepared for a client submission (the submission itself is the purpose)"),
    "compliance_chases.message_refs": ("justified_plaintext", "message ids only"),
    "knowledge_source_versions.body_md": ("justified_plaintext", "tenant knowledge content, not personal data"),
    "knowledge_source_versions.extracted_text": ("justified_plaintext", "tenant knowledge content, not personal data"),
    "knowledge_chunks.text": ("justified_plaintext", "tenant knowledge content, not personal data"),
    "collection_agent_holds.reason": ("justified_plaintext", _OPS),
    "collection_agent_log.summary": ("justified_plaintext", "client-level collections log (no candidate data)"),
    "timesheet_flags.review_note": ("justified_plaintext", _OPS),
    "agent_proposals.decision_note": ("justified_plaintext", "reviewer's decision note; proposal retention"),
    "agent_tool_calls.result_summary": ("payload_store", "redacted summary (``sanitize.redact_for_log``); full payloads live in the sealed payload store"),
    # ---- Phase 5
    "ecosystem_settings.kill_switch_reason": ("justified_plaintext", _OPS),
    "ecosystem_settings.platform_suspended_reason": ("justified_plaintext", _OPS),
    "platform_ecosystem_controls.global_suspended_reason": ("justified_plaintext", _OPS),
    "apps.scope_reasons": ("justified_plaintext", "developer-supplied scope justifications"),
    "apps.suspended_reason": ("justified_plaintext", _OPS),
    "app_installations.suspended_reason": ("justified_plaintext", _OPS),
    "telephony_settings.recording_notice_text": ("justified_plaintext", "tenant notice text, not personal data"),
    "webhook_subscriptions.description": ("justified_plaintext", "integration description written by an admin"),
    "marketplace_listings.pricing_note": ("justified_plaintext", "partner listing content"),
    "marketplace_listings.suspended_reason": ("justified_plaintext", _OPS),
    "marketplace_listing_versions.description_md": ("justified_plaintext", "partner listing content"),
    "marketplace_listing_versions.security_questionnaire": ("justified_plaintext", "partner security answers (business data)"),
    "marketplace_listing_versions.decision_message": ("justified_plaintext", _OPS),
    "marketplace_reviews.message": ("justified_plaintext", "a tenant admin's review of an App (business opinion)"),
    "marketplace_reviews.internal_note": ("justified_plaintext", _OPS),
    "campaign_copy_drafts.body_html": ("justified_plaintext", "marketing copy with merge-field placeholders, never recipient data"),
    "mcp_access_holds.note": ("justified_plaintext", _OPS),
    "access_review_items.note": ("justified_plaintext", "the reviewer's keep/reduce/revoke note; access-review retention + RLS (Epic 79)"),
}


def _free_text_columns() -> dict[str, str]:
    import app.models  # noqa: F401 - registers every model

    out: dict[str, str] = {}
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if cls.__module__.rsplit(".", 1)[-1] not in P4_P5_MODEL_MODULES:
            continue
        table = mapper.local_table
        pii = set(getattr(cls, "__pii_fields__", ()) or ())
        for col in table.columns:
            ty = col.type
            free = isinstance(ty, Text | JSONB) or (isinstance(ty, String) and (ty.length or 0) >= 300)
            if (free and _NAME.search(col.name)) or col.name in pii:
                out[f"{table.name}.{col.name}"] = cls.__module__
    return out


def test_every_free_text_column_is_classified() -> None:
    found = _free_text_columns()
    missing = sorted(k for k in found if k not in FREE_TEXT_INVENTORY)
    assert missing == [], f"classify these P4/P5 free-text columns in FREE_TEXT_INVENTORY: {missing}"
    for key, (kind, reason) in FREE_TEXT_INVENTORY.items():
        assert kind in ("sealed", "payload_store", "justified_plaintext"), key
        assert reason.strip(), key


def test_onboarding_qa_text_is_sealed_only() -> None:
    from app.models.knowledge import OnboardingQaMessage

    cols = set(OnboardingQaMessage.__table__.columns.keys())
    assert {"text_sealed", "feedback_note_sealed"} <= cols
    assert not {"text", "feedback_note"} & cols, "the plaintext columns are dropped by 512_03"
    assert FREE_TEXT_INVENTORY["onboarding_qa_messages.text_sealed"][0] == "sealed"
    assert FREE_TEXT_INVENTORY["onboarding_qa_messages.feedback_note_sealed"][0] == "sealed"


async def test_database_has_no_plaintext_qa_columns(owner_db: object) -> None:
    from sqlalchemy import text

    async with owner_db.connect() as conn:  # type: ignore[attr-defined]
        cols = {r[0] for r in await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'onboarding_qa_messages'"))}
    assert "text_sealed" in cols
    assert not {"text", "feedback_note"} & cols
