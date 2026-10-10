# ruff: noqa: D401, E501, TC003
"""Epic 54's own Copilot capabilities (``core.agentic.register_copilot_capability``) and the conversation modes.

A capability's tools join the Copilot allow-list when the capability is on (C-4: the owning agent's AI feature is on for
the tenant and the user holds ``permission``). Other epics register theirs the same way (64 ``analytics``, 55
``draft_demand``) without importing Copilot code. A conversation **mode** is an agent registered with
``origin="copilot"`` other than ``copilot`` itself (e.g. Epic 64's ``analytics``)."""

from collections.abc import Mapping

from app.core.agentic import (
    AgentSpec,
    CopilotCapability,
    agents,
    copilot_capabilities,
    register_copilot_capability,
)

OWN_CAPABILITIES: tuple[CopilotCapability, ...] = (
    CopilotCapability(
        "schedule_proposal", "54", "Propose interview slots",
        "Prepare interview slot options for a candidate (you confirm before anything is sent)",
        tools=("copilot.propose_slots",),
    ),
    CopilotCapability(
        "submission_pack", "54", "Prepare a submission pack",
        "Assemble a draft submission pack for an application (sending stays with you)",
        tools=("copilot.prepare_pack",),
    ),
)  # fmt: skip

MODE_LABELS: dict[str, str] = {"copilot": "Copilot", "analytics": "Ask about your data"}


def register() -> None:
    existing = copilot_capabilities()
    for cap in OWN_CAPABILITIES:
        if cap.key not in existing:
            register_copilot_capability(cap)


def mode_specs() -> list[AgentSpec]:
    """Registered Copilot modes besides ``copilot`` (agents with ``origin="copilot"``, not placeholders)."""
    return [
        spec
        for key, spec in sorted(agents().items())
        if spec.origin == "copilot" and key != "copilot" and not spec.placeholder and not key.startswith("_")
    ]


def mode_label(agent_key: str) -> str:
    if agent_key in MODE_LABELS:
        return MODE_LABELS[agent_key]
    spec: Mapping[str, AgentSpec] = agents()
    return spec[agent_key].name if agent_key in spec else agent_key


__all__ = ["MODE_LABELS", "OWN_CAPABILITIES", "mode_label", "mode_specs", "register"]
