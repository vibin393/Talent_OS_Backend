# ruff: file-ignore[missing-trailing-period, missing-terminal-punctuation, line-too-long]
"""Copilot wiring (Epic 54), autodiscovered by ``core.hooks``:

* Epic 54's own Copilot capabilities (``schedule_proposal``, ``submission_pack``);
* event subscribers (after commit; each enqueues a ``copilot.*`` task that opens its own tenant session):
  ``AgentRunFinished`` / ``AgentRunFailed`` of a Copilot agent -> store the answer + ``turn.completed|failed``;
  ``ProposalCreated`` (origin ``copilot``) -> ``draft.created``; ``ProposalDecided`` of a Copilot agent -> an event
  message (``draft_confirmed`` / ``draft_failed`` / ``draft_discarded`` / ``draft_expired`` / ``draft_stale``);
* the settings hub entry (``/settings/ai/copilot``).

No new domain events or notification types (A7)."""

from typing import Any

from loguru import logger

from app.core.agentic import agents
from app.core.events import subscribe
from app.services.copilot import capabilities

capabilities.register()


def _copilot_agent(agent_key: str) -> bool:
    spec = agents().get(agent_key)
    return spec is not None and spec.origin == "copilot" and not agent_key.startswith("_")


def _enqueue(task_name: str, *args: Any) -> None:
    from app.tasks import copilot as copilot_tasks
    from app.tasks.runtime import enqueue

    enqueue(getattr(copilot_tasks, task_name), *args)


def _subscribe() -> None:
    from app.events.agents import AgentRunFailed, AgentRunFinished, ProposalCreated, ProposalDecided

    def on_finished(e: Any) -> None:
        if _copilot_agent(e.agent_key):
            _enqueue("on_run_finished", str(e.tenant_id), str(e.run_id))

    def on_created(e: Any) -> None:
        if e.origin == "copilot" and e.run_id is not None:
            _enqueue("on_draft_created", str(e.tenant_id), str(e.run_id), str(e.proposal_id))

    def on_decided(e: Any) -> None:
        if _copilot_agent(e.agent_key) and e.run_id is not None:
            _enqueue("on_draft_decided", str(e.tenant_id), str(e.run_id), str(e.proposal_id), e.outcome)

    on_finished.__name__ = "copilot_on_run_finished"
    on_created.__name__ = "copilot_on_draft_created"
    on_decided.__name__ = "copilot_on_draft_decided"
    subscribe(AgentRunFinished, on_finished)
    subscribe(AgentRunFailed, on_finished)
    subscribe(ProposalCreated, on_created)
    subscribe(ProposalDecided, on_decided)


def _register_settings_entry() -> None:
    try:
        from app.services import settings_service

        settings_service.register_settings_entry(
            "ai", "copilot", "Copilot", "Turn the Copilot on, and set retention, rate limit and draft expiry",
            "/api/v1/copilot/settings", "ai:agent_configure", owner_epic="54",
        )  # fmt: skip
    except Exception:
        logger.exception("Could not register the Copilot settings entry")


_subscribe()
_register_settings_entry()
