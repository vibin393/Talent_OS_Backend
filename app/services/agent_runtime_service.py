# ruff: noqa: E501
"""Agent runtime facade (Epic 53, spec 0.8, AD-72). The only runtime entry point for agents, the Copilot, routers,
tasks and other Phase 4 epics (``agents.*`` / ``copilot`` may import this facade and nothing else of the runtime).

Epic 66 (§0.15, additive): ``OwnerRef`` + ``start_run(..., owner=)`` for portal owners and the external (MCP) session
API ``open_external_session`` / ``execute_external_call`` / ``close_external_session`` (Epic 73's MCP server)."""

from app.schemas.agentic import ExternalCallIn, ExternalCallOut, ExternalSessionIn
from app.services.agent_runtime.admin import (
    acknowledge_enablement,
    agentic_state,
    get_agents,
    platform_set_kill_switch,
    set_action_policy,
    set_agent_enabled,
    set_kill_switch,
    update_config,
)
from app.services.agent_runtime.external import (
    close_external_session,
    execute_external_call,
    open_external_session,
)
from app.services.agent_runtime.principals import scope_context
from app.services.agent_runtime.proposals import bulk_decide, confirm_draft, create_draft_for, discard_draft
from app.services.agent_runtime.proposals import decide as decide_proposal
from app.services.agent_runtime.proposals import get as get_proposal
from app.services.agent_runtime.proposals import list_ as list_proposals
from app.services.agent_runtime.runs import (
    OwnerRef,
    cancel_run,
    effective_mode,
    get_run,
    list_runs,
    purge_run_content,
    read_payload,
    resume,
    start_run,
    tool_catalogue,
)
from app.services.agent_runtime.send_policy import register_grounded_conversation_checker

__all__ = [
    "ExternalCallIn",
    "ExternalCallOut",
    "ExternalSessionIn",
    "OwnerRef",
    "acknowledge_enablement",
    "agentic_state",
    "bulk_decide",
    "cancel_run",
    "close_external_session",
    "confirm_draft",
    "create_draft_for",
    "decide_proposal",
    "discard_draft",
    "effective_mode",
    "execute_external_call",
    "get_agents",
    "get_proposal",
    "get_run",
    "list_proposals",
    "list_runs",
    "open_external_session",
    "platform_set_kill_switch",
    "purge_run_content",
    "read_payload",
    "register_grounded_conversation_checker",
    "resume",
    "scope_context",
    "set_action_policy",
    "set_agent_enabled",
    "set_kill_switch",
    "start_run",
    "tool_catalogue",
    "update_config",
]
