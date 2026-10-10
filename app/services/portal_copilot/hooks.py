# ruff: noqa: E501, D400, D415
"""Portal Copilot wiring (Epic 77), autodiscovered by ``core.hooks``:

* the Candidate implicit-rights resolver for portal-owned runs (``agent_runtime.principals.register_candidate_rights``);
* event subscribers (after commit; each enqueues a ``portal_copilot.*`` task on ``conversations``):
  ``AgentRunFinished`` / ``AgentRunFailed`` of a portal Copilot agent -> store the answer + ``turn.completed|failed``;
  ``ProposalDecided`` of a portal Draft -> an event message;
* the default ``portal_copilot`` consent notice and the settings hub entry ``ai`` / ``portal_copilot``.
"""

from typing import Any

from loguru import logger

from app.core.events import subscribe
from app.services.agent_runtime.principals import register_candidate_rights
from app.services.portal_copilot import consent
from app.services.portal_copilot.principals import candidate_rights

PORTAL_AGENT_KEYS = frozenset({"copilot_client", "copilot_vendor", "copilot_candidate"})


def _enqueue(task_name: str, *args: Any) -> None:
    from app.tasks import portal_copilot as tasks
    from app.tasks.runtime import enqueue

    enqueue(getattr(tasks, task_name), *args)


def _subscribe() -> None:
    from app.events.agents import AgentRunFailed, AgentRunFinished, ProposalDecided

    def on_finished(e: Any) -> None:
        if e.agent_key in PORTAL_AGENT_KEYS:
            _enqueue("on_run_finished", str(e.tenant_id), str(e.run_id))

    def on_decided(e: Any) -> None:
        if e.agent_key in PORTAL_AGENT_KEYS and e.run_id is not None:
            _enqueue("on_draft_decided", str(e.tenant_id), str(e.run_id), str(e.proposal_id))

    on_finished.__name__ = "portal_copilot_on_run_finished"
    on_decided.__name__ = "portal_copilot_on_draft_decided"
    subscribe(AgentRunFinished, on_finished)
    subscribe(AgentRunFailed, on_finished)
    subscribe(ProposalDecided, on_decided)


def _register_settings_entry() -> None:
    try:
        from app.services import settings_service

        settings_service.register_settings_entry(
            "ai", "portal_copilot", "Portal Copilot",
            "Turn the Copilot on for the Client, Vendor and Candidate portals, with limits and retention.",
            "/api/v1/copilot/portal-admin/settings", "ai:agent_configure", owner_epic="77",
        )  # fmt: skip
    except Exception:
        logger.exception("Could not register the portal Copilot settings entry")


register_candidate_rights(candidate_rights)
consent.register_notice()
_subscribe()
_register_settings_entry()
