"""After-commit event handlers of the portal domain (AD-9): each enqueues a Celery task (``tenant_id`` first)."""

from app.core.events import subscribe
from app.events.demands import DemandPublished
from app.events.portal import CareerApplicationSubmitted, DsrRequested

_registered = False


def _demand_published(event: DemandPublished) -> None:
    from app.tasks import portal as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(tasks.refresh_posting_search, str(event.tenant_id), str(event.demand_id))


def _application_submitted(event: CareerApplicationSubmitted) -> None:
    from app.tasks import portal as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(
        tasks.send_apply_confirmation, str(event.tenant_id), str(event.application_id), event.already_applied
    )


def _dsr_requested(event: DsrRequested) -> None:
    from app.tasks import portal as tasks
    from app.tasks.ops_runtime import enqueue

    if event.export_job_id is not None:
        enqueue(tasks.build_candidate_export, str(event.tenant_id), str(event.export_job_id))
    else:
        enqueue(tasks.notify_privacy_contact, str(event.tenant_id), str(event.dsr_id))


def register() -> None:
    global _registered  # process-wide, idempotent
    if _registered:
        return
    subscribe(DemandPublished, _demand_published)
    subscribe(CareerApplicationSubmitted, _application_submitted)
    subscribe(DsrRequested, _dsr_requested)
    from app.services import files_service
    from app.services.portal import site

    files_service.register_access_checker("career_site", site.career_site_access, plural="career-sites")
    _registered = True
