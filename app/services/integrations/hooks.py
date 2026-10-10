# ruff: noqa: E501, D400, D415
"""Integrations wiring (Epic 66, spec 0.10 / 0.17), autodiscovered:

* the ``sync:`` op runner and the Installation job listener in the P2 connector job layer (P2 code never imports
  Phase 5 packages; it calls the registered functions);
* the ``connector_triggers`` outbox sink (public and internal rows);
* the post-discovery Connector Manifest validation (startup fails on an invalid manifest).
"""

from app.core.hooks import register_post_discovery
from app.core.integrations import validate_catalogue
from app.services import public_events_service
from app.services.connectors import registry as connector_registry
from app.services.integrations.runner import SYNC_OP_PREFIX, on_job_outcome, run_sync_job
from app.services.integrations.triggers import connector_triggers_sink


async def _sink(ctx, db, rows) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    await connector_triggers_sink(ctx, db, rows)


connector_registry.register_job_runner(SYNC_OP_PREFIX, run_sync_job)
connector_registry.register_job_listener(on_job_outcome)
if "connector_triggers" not in {s.name for s in public_events_service.sinks()}:
    public_events_service.register_sink("connector_triggers", _sink, include_internal=True)


def _validate_manifests() -> None:
    problems = validate_catalogue()
    if problems:
        msg = "Connector manifests invalid: " + "; ".join(problems)
        raise RuntimeError(msg)


register_post_discovery(_validate_manifests)
