"""Epic 54 definition, capabilities and pure helpers (no DB)."""

import asyncio
import uuid

import pytest

from app.core.agentic import FORBIDDEN_ACTIONS, copilot_capabilities, get_agent, tools, validate_catalogue
from app.core.agentic.copilot import enabled_capabilities
from app.core.permissions import Principal, Scope
from app.services.agent_runtime import state
from app.services.agents.copilot.definition import COPILOT, FORBIDDEN_CEILING_KEYS
from app.services.copilot import context, streaming
from app.services.copilot.settings import unavailable_reason
from app.services.workflow import catalogue


def test_placeholder_replaced_and_catalogue_valid() -> None:
    spec = get_agent("copilot")
    assert spec is COPILOT and not spec.placeholder and spec.origin == "copilot" and spec.owner_epic == "54"
    assert spec.model_tier == "standard" and spec.effort == "low" and spec.fallback == "none"
    assert validate_catalogue(is_human_only=catalogue.is_human_only) == []
    assert all(key in tools() for key in spec.tools)


def test_ceiling_never_holds_forbidden_keys() -> None:
    keys = set(COPILOT.permission_ceiling)
    assert not keys & FORBIDDEN_CEILING_KEYS
    assert not any(k.startswith(("ai:", "settings:")) for k in keys)
    assert not any(k.endswith((":approve", ":reject", ":release", ":export")) for k in keys)
    assert "compensation:view" in keys


def test_allow_list_has_no_star_or_draft_of_draft() -> None:
    allow = set(COPILOT.tools)
    assert "messaging.create_draft" not in allow and "flags.raise" not in allow and "notify.user" not in allow
    for entity, target in FORBIDDEN_ACTIONS:
        assert not any(t.endswith(f".{target}") and entity in t for t in allow)
    for key in allow:
        tool = tools()[key]
        assert tool.transition is None or not catalogue.is_human_only(*tool.transition)
    for key in ("search.global", "candidates.search", "matching.rank", "navigation.screen_for"):
        assert tools()[key].risk == "read" and tools()[key].owner_epic == "54"


def test_capability_tools_only_when_enabled() -> None:
    assert {"schedule_proposal", "submission_pack"} <= set(copilot_capabilities())
    user = Principal(
        type="user", id=uuid.uuid4(), tenant_id=uuid.uuid4(), permissions={"copilot:use": Scope.TENANT}
    )
    off = state.allow_list(COPILOT, {"agentic": True, "agent_copilot": False}, user)
    on = state.allow_list(COPILOT, {"agentic": True, "agent_copilot": True}, user)
    assert (
        "copilot.propose_slots" not in off and "copilot.propose_slots" in on and "copilot.prepare_pack" in on
    )
    keys = {c.key for c in enabled_capabilities({"agent_copilot": True}, lambda _: False)}
    assert "schedule_proposal" in keys and "analytics" not in keys


def test_unavailable_reason_order() -> None:
    base = {
        "has_permission": True,
        "features": {"agentic": True, "agent_copilot": True},
        "tenant_kill": False,
        "global_kill": False,
        "budget_exhausted": False,
    }
    assert unavailable_reason(**base) is None
    assert unavailable_reason(**{**base, "has_permission": False}) == "no_permission"
    assert unavailable_reason(**{**base, "global_kill": True, "tenant_kill": True}) == "global_kill"
    assert unavailable_reason(**{**base, "tenant_kill": True}) == "kill_switch"
    assert unavailable_reason(**{**base, "features": {"agentic": True}}) == "not_enabled"
    assert unavailable_reason(**{**base, "budget_exhausted": True}) == "budget_exhausted"


def test_prior_turns_keep_newest_and_put_summary_first() -> None:
    msgs = [
        ("user", {"text": "q" * 30_000}),
        ("assistant", {"text": "a" * 30_000}),
        ("user", {"text": "latest"}),
        ("event", {"message": "x"}),
    ]
    text, cut = context.prior_turns(msgs, summary="earlier summary", max_chars=40_000)
    assert (
        cut and text.startswith("Summary of earlier turns: earlier summary") and text.endswith("User: latest")
    )
    assert len(text) <= 40_000
    text2, cut2 = context.prior_turns(
        [("user", {"redacted": True, "role": "user"}), ("user", {"text": "hi"})], summary=None
    )
    assert not cut2 and text2 == "User: hi"
    kick, _ = context.build_kickoff(
        question="Q",
        page_context={"route": "/demands"},
        today="2026-10-08",
        user_display_name="Ada",
        prior=[("user", {"text": "hi"})],
    )
    assert kick.inputs["fy_start_month"] == "4" and kick.untrusted["prior_turns"] == "User: hi"


def test_sse_frame_and_heartbeat() -> None:
    assert (
        streaming.frame("1-0", "text.delta", '{"text":"Hi"}')
        == b'id: 1-0\nevent: text.delta\ndata: {"text":"Hi"}\n\n'
    )

    async def collect() -> list[bytes]:
        run_id = uuid.uuid4()
        await streaming.publish(run_id, "turn.started", {"run_id": str(run_id)})
        out: list[bytes] = []
        gen = streaming.sse(run_id, last_event_id=None, heartbeat_s=0.2, timeout_s=1)
        async for chunk in gen:
            out.append(chunk)
            if b": ping" in chunk:
                break
        await gen.aclose()
        return out

    chunks = asyncio.run(collect())
    assert any(b"event: turn.started" in c for c in chunks) and any(c == b": ping\n\n" for c in chunks)


@pytest.mark.parametrize("key", sorted(COPILOT.tools))
def test_tool_descriptions_state_limits(key: str) -> None:
    tool = tools()[key]
    assert len(tool.description) <= 1024 and tool.input_model.model_config.get("extra") == "forbid"
