# ruff: noqa: E501
"""Registers the Copilot ``AgentSpec`` (replaces the foundation placeholder; autodiscovered by ``core.hooks``)."""

from app.core.agentic import register_agent
from app.services.agents.copilot.definition import COPILOT

register_agent(COPILOT)
