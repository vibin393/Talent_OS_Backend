"""Registrations of the connector framework (autodiscovered): importing the package registers every category's
``manual`` (and, outside production, ``fake``) adapter and the Epic 66 manifest categories.

Epic 66 (FR-340): lifting the Ecosystem Kill Switch (``EcosystemKillSwitchChanged(on=False)``) queues
``connectors.release_held`` for the tenant (held inbound webhooks are processed in arrival order)."""

import app.services.connectors  # ruff: ignore[unused-import]  # noqa: F401
from app.core.events import subscribe
from app.events.ecosystem import EcosystemKillSwitchChanged


def _on_kill_switch(event: EcosystemKillSwitchChanged) -> None:
    if event.on:
        return
    from app.tasks import connectors as connector_tasks
    from app.tasks.runtime import enqueue

    enqueue(connector_tasks.release_held, str(event.tenant_id))


subscribe(EcosystemKillSwitchChanged, _on_kill_switch)
