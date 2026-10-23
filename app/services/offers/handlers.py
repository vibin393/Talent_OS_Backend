"""After-commit event handlers of the offers domain: enqueue the Celery tasks (AD-9). Never log PII."""

from loguru import logger

from app.core.events import subscribe
from app.events.offers import OfferAccepted, OfferDeclined, OfferEmailRequested, OfferLetterRequested

_registered = False


def _enqueue(task_name: str, *args: object) -> None:
    import app.tasks.offers  # ruff: ignore[unused-import] - registers the tasks
    from app.core.celery import celery_app

    try:
        celery_app.tasks[task_name].delay(*args)
    except Exception:
        logger.exception("Could not enqueue {} ({})", task_name, args[0] if args else "")


def _on_letter(e: OfferLetterRequested) -> None:
    _enqueue("offers.render_offer_letter", str(e.tenant_id), str(e.offer_id), int(e.version_no))


def _on_email(e: OfferEmailRequested) -> None:
    _enqueue("offers.send_offer_email", str(e.tenant_id), str(e.offer_id), e.kind)


def _on_accepted(e: OfferAccepted) -> None:
    _enqueue("offers.finalize_accepted_offer", str(e.tenant_id), str(e.offer_id))


def _on_declined(e: OfferDeclined) -> None:
    _enqueue("offers.send_offer_email", str(e.tenant_id), str(e.offer_id), "declined_owner")


def _on_tenant(e: object) -> None:
    _enqueue("offers.ensure_defaults", str(getattr(e, "tenant_id", "")))


def register() -> None:
    global _registered  # idempotent process-wide wiring
    if _registered:
        return
    subscribe(OfferLetterRequested, _on_letter)
    subscribe(OfferEmailRequested, _on_email)
    subscribe(OfferAccepted, _on_accepted)
    subscribe(OfferDeclined, _on_declined)
    try:
        from app.events.identity import TenantProvisioned
    except ImportError:  # pragma: no cover
        pass
    else:
        subscribe(TenantProvisioned, _on_tenant)
    _registered = True
