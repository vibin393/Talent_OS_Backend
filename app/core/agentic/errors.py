# ruff: noqa: E501
"""Registry errors of ``app.core.agentic`` (raised at import / startup, never to API callers)."""


class ToolRegistryError(ValueError):
    """A ``ToolSpec`` breaks a registration rule (duplicate, bad key, forbidden risk, missing preview, ...)."""


class AgentRegistryError(ValueError):
    """An ``AgentSpec`` / ``TriggerSpec`` / ``CopilotCapability`` breaks a registration rule."""


__all__ = ["AgentRegistryError", "ToolRegistryError"]
