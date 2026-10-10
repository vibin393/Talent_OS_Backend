"""After-commit event handlers of Epic 12 (AD-9): each enqueues a Celery task with ``tenant_id`` first.

Subscribed when ``app.services.privacy`` is imported.
"""

from app.core.events import subscribe
from app.events.privacy import (
    AIProcessingBasisChanged,
    ConsentWithdrawn,
    ErasureRequested,
    RetentionExecutionRequested,
    SubjectExportRequested,
)
from app.services.ai.budget import AIBudgetThresholdCrossed


def _consent_withdrawn(event: ConsentWithdrawn) -> None:
    if event.purpose != "ai_processing":
        return
    from app.tasks import privacy as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(tasks.purge_ai_derived, str(event.tenant_id), str(event.candidate_id))


def _basis_changed(event: AIProcessingBasisChanged) -> None:
    from app.tasks import privacy as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(tasks.recompute_ai_consent_state, str(event.tenant_id))


def _erasure_requested(event: ErasureRequested) -> None:
    from app.tasks import privacy as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(tasks.run_erasure, str(event.tenant_id), str(event.run_id))


def _export_requested(event: SubjectExportRequested) -> None:
    from app.tasks import privacy as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(tasks.build_subject_export, str(event.tenant_id), str(event.dsr_id))


def _retention_requested(event: RetentionExecutionRequested) -> None:
    from app.tasks import privacy as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(tasks.execute_retention_run, str(event.tenant_id), str(event.run_id))


def _budget_alert(event: AIBudgetThresholdCrossed) -> None:
    from app.tasks import platform as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(tasks.notify_budget_alert, str(event.tenant_id), event.threshold, event.period, event.used, event.budget)


def _tenant_export(event: object) -> None:
    from app.tasks import platform as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(tasks.export_tenant, str(event.tenant_id), str(event.export_id))  # type: ignore[attr-defined]


def _placement_created(event: object) -> None:
    """Epic 35 (FR-179): record the Worker's ``engagement`` basis (source ``placement``)."""
    from app.tasks import privacy as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(tasks.record_engagement_consent, str(event.tenant_id), str(event.candidate_id))  # type: ignore[attr-defined]


def install() -> None:
    from app.events.placements import PlacementCreated
    from app.services.tenant_lifecycle_service import TenantExportRequested

    subscribe(PlacementCreated, _placement_created)

    subscribe(ConsentWithdrawn, _consent_withdrawn)
    subscribe(AIProcessingBasisChanged, _basis_changed)
    subscribe(ErasureRequested, _erasure_requested)
    subscribe(SubjectExportRequested, _export_requested)
    subscribe(RetentionExecutionRequested, _retention_requested)
    subscribe(AIBudgetThresholdCrossed, _budget_alert)
    subscribe(TenantExportRequested, _tenant_export)
