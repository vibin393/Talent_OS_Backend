# ruff: file-ignore[line-too-long]
"""Epic 31 registrations, called once from ``placements/hooks.py`` (autodiscovered).

* Approval subjects ``placement_extension`` and ``extension_exception`` (human deciders, AD-24).
* Event subscriptions -> ``placements_exit.refresh`` (queue ``interactive``): ``TimesheetApproved``,
  ``ExpenseClaimDecided`` (by placement), ``PayrollExportFinalized`` (all open cases), ``InvoiceIssued`` (open
  cases of the client); ``SignatureRequestCompleted(subject_kind='offboarding_item')`` -> letter item done.
  Upper-domain events are imported by name (``importlib``) so placements never imports their services.
* Notification types ``placements.ending_soon`` (AM + recruiter), ``placements.extension_status_changed``,
  ``placements.offboarding_item_overdue``; workflow triggers ``placements.extension_status_changed``,
  ``placements.ending_soon``, ``placements.offboarding_overdue_item``, ``placements.offboarding_status_changed``.
* SLA milestone ``extension.client_confirmation``. Workflow actors can never confirm or close (★ status machines).
"""

import importlib
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe

_done = False


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _refresh(tenant_id: Any, placement_id: Any = None, client_id: Any = None) -> None:
    from app.tasks import placements_exit as tasks
    from app.tasks.runtime import enqueue

    enqueue(
        tasks.refresh,
        str(tenant_id),
        str(placement_id) if placement_id else None,
        str(client_id) if client_id else None,
    )


def _on_placement_event(event: Any) -> None:
    _refresh(event.tenant_id, placement_id=getattr(event, "placement_id", None))


def _on_payroll(event: Any) -> None:
    _refresh(event.tenant_id)


def _on_invoice(event: Any) -> None:
    _refresh(event.tenant_id, client_id=getattr(event, "client_id", None))


def _on_signature(event: Any) -> None:
    if getattr(event, "subject_kind", None) != "offboarding_item":
        return
    from app.tasks import placements_exit as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.signature_completed, str(event.tenant_id), str(event.subject_id), str(event.document_id))


_UPPER = {
    "app.events.time": (("TimesheetApproved", _on_placement_event),),
    "app.events.expenses": (
        ("ExpenseClaimDecided", _on_placement_event),
        ("ExpenseClaimSettled", _on_placement_event),
    ),
    "app.events.payroll": (("PayrollExportFinalized", _on_payroll),),
    "app.events.billing": (("InvoiceIssued", _on_invoice),),
    "app.events.documents": (("SignatureRequestCompleted", _on_signature),),
}


def _subscribe() -> None:
    for module_name, pairs in _UPPER.items():
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover
            continue
        for name, handler in pairs:
            cls = getattr(module, name, None)
            if cls is not None:
                subscribe(cls, handler)


# ---------------------------------------------------------------- notifications
async def _listed(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]  # NOSONAR
    return {u for u in (_uuid(x) for x in payload.get("recipient_ids") or []) if u}


async def _owner(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]  # NOSONAR
    oid = _uuid(payload.get("owner_id"))
    return {oid} if oid else set()


async def _am(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.models.placements import Placement

    pid = _uuid(payload.get("placement_id"))
    if pid is None:
        return set()
    row = (
        await db.execute(
            select(Placement.account_manager_user_id, Placement.recruiter_user_id).where(Placement.id == pid)
        )
    ).first()
    return {u for u in (row or ()) if u}


async def _human(db: AsyncSession, placement_id: uuid.UUID | None) -> str:
    from app.models.placements import Placement

    if placement_id is None:
        return "A placement"
    human = (
        await db.execute(select(Placement.human_id).where(Placement.id == placement_id))
    ).scalar_one_or_none()
    return f"Placement {human}" if human else "A placement"


def _content(title: str, placement_id: uuid.UUID | None, path: str | None = None) -> Any:
    from app.services.notifications_service import Content

    return Content(
        title=title[:300],
        entity_type="placement" if placement_id else None,
        entity_id=placement_id,
        link_path=path or (f"/placements/{placement_id}" if placement_id else None),
    )


async def _render_ending(db: AsyncSession, payload: Any) -> Any:
    pid = _uuid(payload.get("placement_id"))
    return _content(
        f"{await _human(db, pid)} ends on {payload.get('end_date')} ({payload.get('days_left')} days left)",
        pid,
        f"/placements/{pid}?tab=extension" if pid else None,
    )


async def _render_extension(db: AsyncSession, payload: Any) -> Any:
    pid = _uuid(payload.get("placement_id"))
    return _content(f"{await _human(db, pid)}: extension {payload.get('to_status')}".replace("_", " "), pid)


async def _render_overdue(db: AsyncSession, payload: Any) -> Any:
    pid = _uuid(payload.get("placement_id"))
    return _content(
        f"{await _human(db, pid)}: an offboarding item is overdue",
        pid,
        f"/offboarding/{payload.get('case_id')}",
    )


def _register_notifications() -> None:
    from app.events.placements import ExtensionStatusChanged
    from app.services.notifications_service import (
        NotificationType,
        register_event_source,
        register_notification_type,
    )
    from app.services.placements import exit_events as ev

    types = {
        "placements.ending_soon": (
            NotificationType("Placement ending soon", "Workforce", "both", False, _listed, _render_ending),
            ev.PlacementEndingSoon,
        ),
        "placements.extension_status_changed": (
            NotificationType(
                "Extension status changed", "Workforce", "in_app", False, _am, _render_extension
            ),
            ExtensionStatusChanged,
        ),
        "placements.offboarding_item_overdue": (
            NotificationType("Offboarding item overdue", "Workforce", "both", False, _owner, _render_overdue),
            ev.OffboardingItemOverdue,
        ),
    }
    for key, (definition, event_cls) in types.items():
        try:
            register_notification_type(key, definition)
        except Exception:
            logger.debug("notification type {} already registered", key)
            continue
        try:
            register_event_source(event_cls, key)
        except Exception:
            logger.debug("event source {} already registered", key)


def _register_triggers() -> None:
    from app.services.workflow.registry import Trigger, register_trigger

    core = "app.events.placements"
    local = "app.services.placements.exit_events"
    for trig in (
        Trigger(
            "placements.extension_status_changed",
            "Extension status changed",
            core,
            "ExtensionStatusChanged",
            "placement",
            "placement_id",
        ),
        Trigger(
            "placements.ending_soon",
            "Placement ending soon",
            local,
            "PlacementEndingSoon",
            "placement",
            "placement_id",
        ),
        Trigger(
            "placements.offboarding_overdue_item",
            "Offboarding item overdue",
            local,
            "OffboardingItemOverdue",
            "placement",
            "placement_id",
        ),
        Trigger(
            "placements.offboarding_status_changed",
            "Offboarding case status changed",
            core,
            "OffboardingCaseStatusChanged",
            "placement",
            "placement_id",
        ),
    ):
        try:
            register_trigger(trig.key, trig)
        except Exception:
            logger.debug("trigger {} already registered", trig.key)


def _register_subjects() -> None:
    from app.services.approvals import register_approval_subject
    from app.services.placements import extensions

    register_approval_subject(
        extensions.SUBJECT,
        on_final_decided=extensions.on_extension_decided,
        describe=extensions.describe,
        auto_approve_calls_back=True,
    )
    register_approval_subject(
        extensions.EXCEPTION_SUBJECT,
        on_final_decided=extensions.on_exception_decided,
        describe=extensions.describe,
        auto_approve_calls_back=True,
    )


def _register_sla() -> None:
    from app.services.sla.registry import SlaMilestoneDef, milestone, register_sla_milestone

    if milestone("extension", "client_confirmation") is None:
        register_sla_milestone(
            "extension", "client_confirmation", SlaMilestoneDef("Extension client confirmation")
        )


async def _file_access(ctx: Any, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:
    """Evidence files attached to extensions (recorded client responses) / offboarding cases (item evidence)."""
    from app.services.placements import extensions, offboarding

    principal = ctx.principal
    if getattr(principal, "type", None) != "user":
        return False
    try:
        if write:
            ext_ok = principal.has("extension:write")
            case_ok = principal.has("offboarding:write")
        else:
            ext_ok = case_ok = principal.has("placement:read")
        if ext_ok:
            try:
                await extensions.get(ctx, db, entity_id)
                return True
            except Exception:  # nosec B110 - best-effort; a failure here is non-fatal by design
                pass
        if case_ok:
            await offboarding.get_detail(ctx, db, entity_id)
            return True
    except Exception:
        return False
    return False


def _register_files() -> None:
    from app.services import files_service

    files_service.register_access_checker("extension", _file_access, plural="extensions")
    files_service.register_access_checker("offboarding_case", _file_access, plural="offboarding-cases")


def register() -> None:
    global _done
    if _done:
        return
    _done = True
    for step in (
        _register_subjects,
        _subscribe,
        _register_notifications,
        _register_triggers,
        _register_sla,
        _register_files,
    ):
        try:
            step()
        except Exception:
            logger.exception("Epic 31 registration {} failed", step.__name__)
