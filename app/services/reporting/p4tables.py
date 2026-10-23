# ruff: file-ignore[line-too-long]
"""Phase 4 read-model guards (Epic 65 A4, AD-1 read-model exception; see ``common`` / ``p2tables``).

Reporting imports **no** Phase 4 service or model module (``p4-below``): every Phase 4 table a tile reads is a
lightweight ``sqlalchemy.table()`` projection registered in ``common.OPTIONAL_TABLES`` with the columns the SQL needs,
so ``available()`` hides a tile whose table (or column) is absent (``unavailable``). Content columns (messages,
transcripts, Proposal args / preview, Copilot text) are never projected.
"""

from sqlalchemy.sql.expression import TableClause

from app.services.reporting import common
from app.services.reporting.p2tables import _required, _t

agent_runs = _t(
    "agent_runs",
    "id:u agent_key:s origin:s trigger_kind:s status:s reason_code:s owner_user_id:u subject_type:s subject_id:u "
    "step_count:i tool_call_count:i input_tokens:i output_tokens:i cost_micros:i tainted:b policy_violations:i "
    "fallback_task_id:u created_at:t started_at:t finished_at:t",
)
agent_tool_calls = _t("agent_tool_calls", "id:u run_id:u tool_key:s risk:s mode:s outcome:s created_at:t")
agent_proposals = _t(
    "agent_proposals",
    "id:u origin:s agent_key:s tool_key:s status:s edited:b owner_user_id:u assignee_user_id:u created_at:t "
    "decided_at:t expires_at:t",
)
agent_configs = _t("agent_configs", "agent_key:s monthly_token_budget:i")
ai_actions_p4 = _t(
    "ai_actions",
    "id:u kind:s agent_key:s agent_run_id:u input_tokens:i output_tokens:i cache_read_tokens:i cost_micros:i created_at:t",
)
copilot_feedback = _t("copilot_feedback", "id:u user_id:u agent_key:s rating:s created_at:t")
copilot_messages = _t("copilot_messages", "conversation_id:u role:s run_id:u created_at:t")
proposal_review_signals = _t(
    "proposal_review_signals",
    "id:u proposal_id:u user_id:u decision:s decision_ms:i rubber_stamp:b decided_at:t",
)
scheduling_requests = _t(
    "scheduling_requests", "id:u owner_user_id:u status:s created_at:t updated_at:t booked_interview_id:u"
)
screening_conversations = _t(
    "screening_conversations", "id:u owner_user_id:u status:s invited_at:t completed_at:t created_at:t"
)
voice_sessions = _t("voice_sessions", "id:u owner_user_id:u status:s created_at:t reviewed_at:t")
onboarding_qa_messages = _t("onboarding_qa_messages", "id:u role:s feedback:s created_at:t")
analytics_answers = _t("analytics_answers", "id:u saved_report_id:u created_at:t")
agent_shortlist_entries = _t("agent_shortlist_entries", "id:u demand_id:u status:s created_at:t")
submission_packs = _t("submission_packs", "id:u status:s created_at:t")
audit_logs_p4 = _t("audit_logs", "id:u actor_type:s action:s entity_type:s agent_run_id:u occurred_at:t")
ai_tenant_settings_p4 = _t("ai_tenant_settings", "agentic_enabled_at:t monthly_token_budget:i")

P4_TABLES: tuple[TableClause, ...] = (
    agent_runs, agent_tool_calls, agent_proposals, agent_configs, ai_actions_p4, copilot_feedback, copilot_messages,
    proposal_review_signals, scheduling_requests, screening_conversations, voice_sessions, onboarding_qa_messages,
    analytics_answers, agent_shortlist_entries, submission_packs,
)  # fmt: skip
# ``ai_actions`` / ``audit_logs`` / ``ai_tenant_settings`` are P1 tables whose Phase 4 columns ship with the foundation
# (400_04); they are projected but not guarded.
for _tbl in P4_TABLES:
    common.OPTIONAL_TABLES.setdefault(_tbl.name, _required(_tbl))

__all__ = [
    "P4_TABLES", "agent_configs", "agent_proposals", "agent_runs", "agent_shortlist_entries", "agent_tool_calls",
    "ai_actions_p4", "ai_tenant_settings_p4", "analytics_answers", "audit_logs_p4", "copilot_feedback",
    "copilot_messages", "onboarding_qa_messages", "proposal_review_signals", "scheduling_requests",
    "screening_conversations", "submission_packs", "voice_sessions",
]  # fmt: skip
