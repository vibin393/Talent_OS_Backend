# ruff: noqa: E501
"""Epic 62 tool catalogue guards (static): every tool registered with its risk, egress tools declare a send intent,
no tool performs a transition, onboarding tools take no candidate id, BGV tools take no address, and the agents'
allow-lists contain no ★ action. Behaviour is covered with the Fake LLM under tests/agents/{compliance,bgv,onboarding}."""

from app.core.agentic import agents, tools
from app.core.hooks import autodiscover

E62 = {
    "compliance.get_chase_plan": "read",
    "compliance.send_chase_reminder": "low",
    "compliance.escalate_chase": "low",
    "compliance.get_risk_list": "read",
    "bgv.get_case_summary": "read",
    "bgv.list_overdue_checks": "read",
    "bgv.request_provider_status": "low",
    "bgv.send_provider_follow_up": "low",
    "bgv.draft_provider_message": "high",
    "bgv.request_candidate_documents": "high",
    "bgv.post_case_summary": "low",
    "onboarding.get_thread": "read",
    "onboarding.get_my_case": "read",
    "onboarding.reply_grounded": "low",
    "onboarding.hand_off": "low",
    "documents.list_my_signature_requests": "read",
}
STAR_WORDS = ("waive", "verify", "adjudicat", "override", "joined", "approve", "reject")


def test_tools_registered_with_risk_and_owner() -> None:
    autodiscover("app.services")
    reg = tools()
    for key, risk in E62.items():
        spec = reg[key]
        assert spec.risk == risk, key
        assert spec.owner_epic == "62" and spec.transition is None, key
        if risk != "read":
            assert spec.preview is not None, key
        if spec.egress:
            assert spec.send_intent is not None, key
    assert reg["compliance.send_chase_reminder"].risk_fn is not None
    assert reg["bgv.send_provider_follow_up"].risk_fn is not None
    assert reg["onboarding.reply_grounded"].egress and not reg["onboarding.hand_off"].egress
    assert not reg["bgv.request_provider_status"].egress


def test_no_candidate_or_address_arguments() -> None:
    autodiscover("app.services")
    reg = tools()
    for key in E62:
        fields = set(reg[key].input_model.model_fields)
        if key.startswith(("onboarding.", "documents.")):
            assert "candidate_id" not in fields and "thread_id" in fields, key
        if key.startswith("bgv."):
            assert not fields & {"email", "address", "to", "recipient", "contact_email"}, key


def test_allow_lists_have_no_star_actions() -> None:
    autodiscover("app.services")
    for key in ("compliance", "bgv", "onboarding"):
        spec = agents()[key]
        assert not spec.placeholder and spec.owner_epic == "62"
        for tool in spec.tools:
            assert tool in tools(), tool
            assert not any(w in tool for w in STAR_WORDS), tool
    assert agents()["onboarding"].origin == "conversation"
    assert "knowledge.search" in agents()["onboarding"].tools
