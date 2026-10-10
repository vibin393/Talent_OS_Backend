# ruff: noqa: E501
"""Registrations of the ``projects`` domain (autodiscovered by ``core.hooks``; Epic 45, spec A6).

* Human-only ★ transition ``deliverable -> accepted`` (FR-257; enforced by ``deliverables.accept``).
* Notification types ``deliverable_submitted`` (Client Users holding ``deliverables:accept`` -- portal deep link
  ``/client/projects/{project_id}`` -- and the Project owner), ``deliverable_decided`` (domain-local: owner and the
  Client's Account Manager, in-app) and ``project_budget_threshold`` (owner, Delivery Managers of the BU, Account
  Manager).
* Activity entity ``project`` (notes / attachments / timeline); file access for ``project`` and ``deliverable``.
* ``PlacementEnded`` / ``PlacementDropped`` -> end the Placement's assignments; ``InvoiceIssued`` /
  ``CreditNoteIssued`` / ``TimesheetApproved`` -> rebuild the burn of the affected projects.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import subscribe
from app.services import activity_service, files_service
from app.services.notifications_service import (
    Content,
    NotificationType,
    Recipient,
    register_event_source,
    register_notification_type,
)
from app.services.workflow.catalogue import register_human_only_transitions

register_human_only_transitions("deliverable", ["accepted"])


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _project(db: AsyncSession, payload: Any) -> Any:
    from app.models.projects import Project

    pid = _uuid(payload.get("project_id"))
    if pid is None:
        return None
    return (await db.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()


async def _account_manager(db: AsyncSession, client_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.clients import Client

    return (await db.execute(select(Client.owner_id).where(Client.id == client_id))).scalar_one_or_none()


# ---------------------------------------------------------------- notifications
async def _submitted_recipients(db: AsyncSession, payload: Any) -> set[Any]:
    from app.models.external_auth import ClientUser, ClientUserScope
    from app.models.projects import ProjectAssignment

    project = await _project(db, payload)
    if project is None:
        return set()
    out: set[Any] = set()
    if project.owner_id:
        out.add(Recipient("user", project.owner_id))
    users = (
        await db.execute(
            select(ClientUser).where(
                ClientUser.client_id == project.client_id,
                ClientUser.status == "active",
                ClientUser.portal_permissions.contains(["deliverables:accept"]),
            )
        )
    ).scalars()
    placements = set(
        (
            await db.execute(
                select(ProjectAssignment.placement_id).where(ProjectAssignment.project_id == project.id)
            )
        ).scalars()
    )
    for cu in users:
        if cu.restricted:
            scoped = set(
                (
                    await db.execute(
                        select(ClientUserScope.subject_id).where(
                            ClientUserScope.client_user_id == cu.id,
                            ClientUserScope.subject_kind == "placement",
                        )
                    )
                ).scalars()
            )
            if not scoped & placements:
                continue
        out.add(Recipient("client_user", cu.id))
    return out


async def _render_submitted(db: AsyncSession, payload: Any) -> Content:
    from app.models.projects import Deliverable

    project = await _project(db, payload)
    did = _uuid(payload.get("deliverable_id"))
    name = (
        (await db.execute(select(Deliverable.name).where(Deliverable.id == did))).scalar_one_or_none()
        if did
        else None
    )
    return Content(
        title=f"Deliverable submitted for acceptance: {name or ''} ({project.name if project else 'project'})"[
            :300
        ],
        entity_type="project",
        entity_id=project.id if project else None,
        link_path=f"/projects/{project.id}" if project else None,
    )


async def _decided_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    project = await _project(db, payload)
    if project is None:
        return set()
    out = {project.owner_id} if project.owner_id else set()
    am = await _account_manager(db, project.client_id)
    if am:
        out.add(am)
    return out


async def _render_decided(db: AsyncSession, payload: Any) -> Content:
    from app.models.projects import Deliverable

    project = await _project(db, payload)
    did = _uuid(payload.get("deliverable_id"))
    name = (
        (await db.execute(select(Deliverable.name).where(Deliverable.id == did))).scalar_one_or_none()
        if did
        else None
    )
    outcome = str(payload.get("outcome") or "decided")
    return Content(
        title=f"Deliverable {name or ''} {outcome} by the Client"[:300],
        entity_type="project",
        entity_id=project.id if project else None,
        link_path=f"/projects/{project.id}" if project else None,
    )


async def _threshold_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.models.identity import User
    from app.services import identity_bridge

    project = await _project(db, payload)
    if project is None:
        return set()
    out = {project.owner_id} if project.owner_id else set()
    am = await _account_manager(db, project.client_id)
    if am:
        out.add(am)
    writers = {u["id"] for u in await identity_bridge.users_with_permission(db, "project:write")}
    if writers and project.business_unit_id is not None:
        same_bu = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(writers), User.business_unit_id == project.business_unit_id
                    )
                )
            ).scalars()
        )
        out |= same_bu
    return out


async def _render_threshold(db: AsyncSession, payload: Any) -> Content:
    project = await _project(db, payload)
    return Content(
        title=(
            f"{project.human_id if project else 'Project'}: {payload.get('dimension')} budget reached "
            f"{payload.get('threshold')}%"
        )[:300],
        entity_type="project",
        entity_id=project.id if project else None,
        link_path=f"/projects/{project.id}" if project else None,
    )


def _register_notifications() -> None:
    from app.events.projects import (
        DeliverableAccepted,
        DeliverableRejected,
        DeliverableSubmitted,
        ProjectBudgetThresholdReached,
    )

    types: dict[str, NotificationType] = {
        "deliverable_submitted": NotificationType(
            "Deliverable submitted for acceptance",
            "Projects",
            "both",
            False,
            _submitted_recipients,
            _render_submitted,
            portal_path="/client/projects/{project_id}",
            external=True,
        ),
        "deliverable_decided": NotificationType(
            "Deliverable accepted or rejected",
            "Projects",
            "in_app",
            False,
            _decided_recipients,
            _render_decided,
        ),
        "project_budget_threshold": NotificationType(
            "Project budget threshold reached",
            "Projects",
            "both",
            False,
            _threshold_recipients,
            _render_threshold,
            internal_only=True,
        ),
    }
    for key, definition in types.items():
        try:
            register_notification_type(key, definition)
        except Exception:
            logger.debug("notification type {} already registered", key)
            return
    register_event_source(DeliverableSubmitted, "deliverable_submitted")
    register_event_source(DeliverableAccepted, "deliverable_decided", {"outcome": "accepted"})
    register_event_source(DeliverableRejected, "deliverable_decided", {"outcome": "rejected"})
    register_event_source(ProjectBudgetThresholdReached, "project_budget_threshold")


_register_notifications()


# ---------------------------------------------------------------- activity / files
async def _project_access(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:
    from app.core.errors import NotFound
    from app.core.permissions import Principal
    from app.services.projects.common import READ, WRITE, get_project

    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user":
        return False
    try:
        await get_project(ctx, db, entity_id, permission=WRITE if write else READ)
    except NotFound:
        return False
    return True


async def _deliverable_access(
    ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool
) -> bool:
    from app.models.projects import Deliverable

    project_id = (
        await db.execute(select(Deliverable.project_id).where(Deliverable.id == entity_id))
    ).scalar_one_or_none()
    return project_id is not None and await _project_access(ctx, db, project_id, write)


activity_service.register_entity("project", _project_access, plural="projects")
files_service.register_access_checker("project", _project_access, plural="projects")
files_service.register_access_checker("deliverable", _deliverable_access, plural="deliverables")


# ---------------------------------------------------------------- P2 events
def _on_placement_ended(event: Any) -> None:
    from app.tasks.projects import placement_closed
    from app.tasks.runtime import enqueue

    end = getattr(event, "end_date", None)
    if end is None:
        from app.services.projects.common import today

        end = today()
    enqueue(
        placement_closed,
        str(event.tenant_id),
        str(event.placement_id),
        end.isoformat(),
        getattr(event, "reason", None),
    )


def _on_invoice(event: Any) -> None:
    from app.tasks.projects import rebuild_burn_for
    from app.tasks.runtime import enqueue

    enqueue(rebuild_burn_for, str(event.tenant_id), "invoice", str(event.invoice_id))


def _on_timesheet(event: Any) -> None:
    from app.tasks.projects import rebuild_burn_for
    from app.tasks.runtime import enqueue

    enqueue(rebuild_burn_for, str(event.tenant_id), "placement", str(event.placement_id))


def _subscribe() -> None:
    from app.events.billing import CreditNoteIssued, InvoiceIssued
    from app.events.placements import PlacementDropped, PlacementEnded
    from app.events.time import TimesheetApproved

    subscribe(PlacementEnded, _on_placement_ended)
    subscribe(PlacementDropped, _on_placement_ended)
    subscribe(InvoiceIssued, _on_invoice)
    subscribe(CreditNoteIssued, _on_invoice)
    subscribe(TimesheetApproved, _on_timesheet)


_subscribe()
