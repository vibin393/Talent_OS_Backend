"""The 15 shipped agent keys (FR-264) as disabled placeholders (Epic 53). Owner epics replace them once with
``register_agent`` from ``app/services/agents/<key>/hooks.py``; a placeholder cannot be enabled
(409 ``agent_not_available``).

Epic 66 (§0.15) adds the Phase 5 keys: ``mcp`` (origin ``external``; its allow-list and permission ceiling are
the MCP exposures, frozen after hooks discovery by ``refresh_mcp_spec``), ``copilot_client`` /
``copilot_vendor`` / ``copilot_candidate`` (origin ``portal``, Epic 77) and ``campaign_copy`` (Epic 78)."""

from types import MappingProxyType

from app.core.agentic.agents import AgentSpec, agents, register_agent
from app.core.agentic.exposure import exposures
from app.core.agentic.tools import tools
from app.core.agentic.types import RunOrigin
from app.core.permissions import Scope

OWNERS: dict[str, tuple[str, str]] = {
    "copilot": ("54", "Recruitment Copilot"),
    "demand": ("55", "Demand Agent"),
    "sourcing": ("56", "Sourcing & Matching Agent"),
    "screening": ("57", "Screening Agent"),
    "voice_screening": ("58", "Voice Screening Agent"),
    "scheduling": ("59", "Scheduling Agent"),
    "interview_assist": ("60", "Interview Assistant"),  # C-17: feature key agent_interview_assist
    "submission": ("61", "Submission Agent"),
    "compliance": ("62", "Compliance Agent"),
    "bgv": ("62", "BGV Agent"),
    "onboarding": ("62", "Onboarding Agent"),
    "timesheet": ("63", "Timesheet Agent"),
    "billing": ("63", "Billing Agent"),
    "collections": ("63", "Collections Agent"),
    "analytics": ("64", "Analytics Agent"),
}

# Epic 66 (§0.15): Phase 5 keys -> (owner epic, name, origin).
P5_OWNERS: dict[str, tuple[str, str, RunOrigin]] = {
    "mcp": ("73", "MCP client", "external"),
    "copilot_client": ("77", "Client Portal Copilot", "portal"),
    "copilot_vendor": ("77", "Vendor Portal Copilot", "portal"),
    "copilot_candidate": ("77", "Candidate Copilot", "portal"),
    "campaign_copy": ("78", "Campaign Copy Agent", "agent"),
}
PORTAL_AGENT_KEYS: frozenset[str] = frozenset({"copilot_client", "copilot_vendor", "copilot_candidate"})

# FR-379 / FR-380 caps of an MCP session (Epic 73 enforces them; exceeding the candidate cap pauses + alerts).
MCP_CAPS: MappingProxyType[str, int] = MappingProxyType(
    {
        "calls_per_minute_per_session": 60,
        "calls_per_user_per_day": 2_000,
        "proposals_per_hour_per_session": 25,
        "distinct_candidates_per_session_hour": 500,
    }
)


def _origin(key: str) -> RunOrigin:
    return "copilot" if key == "copilot" else "agent"


def mcp_spec() -> AgentSpec:
    """Build the ``mcp`` placeholder: tools = the MCP exposures, ceiling = the union of their permissions."""
    keys = tuple(sorted(exposures("mcp")))
    catalogue = tools()
    ceiling: dict[str, Scope] = {}
    for key in keys:
        tool = catalogue.get(key)
        if tool is not None:
            ceiling.update(dict.fromkeys(tool.permissions, Scope.TENANT))
    epic, name, origin = P5_OWNERS["mcp"]
    return AgentSpec(
        key="mcp",
        version=1,
        owner_epic=epic,
        name=name,
        purpose="External MCP client sessions (no prompt; the client drives the tool calls).",
        prompt_id="agent_mcp",
        tools=keys,
        permission_ceiling=ceiling,
        default_mode="suggest",
        origin=origin,
        placeholder=True,
    )


def refresh_mcp_spec() -> None:
    """Re-freeze the ``mcp`` placeholder from the current exposures.

    Runs post-discovery (and in tests after ``expose_tool``). A real ``mcp`` AgentSpec (Epic 73) is
    never replaced."""
    current = agents().get("mcp")
    if current is not None and not current.placeholder:
        return
    register_agent(mcp_spec())


def register_placeholders() -> None:
    for key, (epic, name) in OWNERS.items():
        register_agent(
            AgentSpec(
                key=key,
                version=1,
                owner_epic=epic,
                name=name,
                purpose=f"Placeholder -- delivered by Epic {epic}.",
                prompt_id=f"agent_{key}",
                origin=_origin(key),
                placeholder=True,
            )
        )
    for key, (epic, name, origin) in P5_OWNERS.items():
        if key == "mcp":
            register_agent(mcp_spec())
            continue
        register_agent(
            AgentSpec(
                key=key,
                version=1,
                owner_epic=epic,
                name=name,
                purpose=f"Placeholder -- delivered by Epic {epic}.",
                prompt_id=f"agent_{key}",
                origin=origin,
                placeholder=True,
            )
        )


__all__ = [
    "MCP_CAPS",
    "OWNERS",
    "P5_OWNERS",
    "PORTAL_AGENT_KEYS",
    "mcp_spec",
    "refresh_mcp_spec",
    "register_placeholders",
]
