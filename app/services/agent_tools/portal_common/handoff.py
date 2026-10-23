# ruff: file-ignore[line-too-long]
"""Hand-offs to a person (Epic 77 A3 / A6): the write side of the portal "message" and "ask your recruiter" drafts.

Runs only when the portal principal confirms the Draft. Each creates an internal Task (owner = the Account Manager,
the Vendor manager or the Application owner; fallback the tenant's ``handoff_queue_role_key`` role, then a Tenant
Admin) whose notes hold only the text the principal reviewed (<= 1,000 chars). The client portal has no message
channel to the Account Manager and the P2/P4 candidate messaging path is staff-to-candidate, so both use the Task
fallback the spec allows (Known gap 2); the Task assignment notifies the assignee."""

import uuid

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.core.events import emit
from app.events.portal_copilot import PortalCopilotHandoff
from app.schemas.tasks import TaskCreate
from app.services import directory_service, entity_refs, tasks_service

NOTE_MAX = 1000
_clients = table("clients", column("id"), column("owner_id"), column("name"))
_vendors = table("vendors", column("id"), column("owner_id"))
_settings = table("portal_copilot_settings", column("handoff_queue_role_key"))
_applications = table("applications", column("id"), column("candidate_id"), column("owner_id"))


async def _active(db: AsyncSession, user_id: uuid.UUID | None) -> uuid.UUID | None:
    if user_id is None:
        return None
    return user_id if user_id in set(await entity_refs.active_user_ids(db, [user_id])) else None


async def _fallback(db: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    del tenant_id
    queue_role = (await db.execute(select(_settings.c.handoff_queue_role_key))).scalar_one_or_none()
    for role_key in (queue_role, "tenant_admin"):
        if not role_key:
            continue
        role_id = await directory_service.role_id_by_key(db, role_key)
        if role_id is None:
            continue
        users = sorted(await directory_service.user_ids_with_role(db, role_id))
        active = sorted(await entity_refs.active_user_ids(db, users)) if users else []
        if active:
            return active[0]
    raise NotFound("No one is available to take this request")


async def _task(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    title: str,
    text: str,
    *,
    entity: tuple[str, uuid.UUID] | None,
    source: str,
) -> uuid.UUID:
    data = TaskCreate.model_construct(
        title=title[:200], notes=text.strip()[:NOTE_MAX], owner_id=owner_id,
        entity_type=entity[0] if entity else None, entity_id=entity[1] if entity else None,
        due_at=None, due_all_day=False, priority="normal", reminder_at=None,
    )  # fmt: skip
    out = await tasks_service.create_system_task(RequestContext(tenant_id=tenant_id), db, data, source=source)
    return out.id


async def message_account_manager(tenant_id: uuid.UUID, client_id: uuid.UUID, text: str) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        owner = (
            await db.execute(select(_clients.c.owner_id).where(_clients.c.id == client_id))
        ).scalar_one_or_none()
        assignee = await _active(db, owner) or await _fallback(db, tenant_id)
        return await _task(
            db,
            tenant_id,
            assignee,
            "Client question from the portal assistant",
            text,
            entity=("client", client_id),
            source="client_portal",
        )


async def message_vendor_manager(tenant_id: uuid.UUID, vendor_id: uuid.UUID, text: str) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        owner = (
            await db.execute(select(_vendors.c.owner_id).where(_vendors.c.id == vendor_id))
        ).scalar_one_or_none()
        assignee = await _active(db, owner) or await _fallback(db, tenant_id)
        return await _task(
            db,
            tenant_id,
            assignee,
            "Vendor question from the portal assistant",
            text,
            entity=None,
            source="agent",
        )


async def candidate_handoff(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, application_id: uuid.UUID | None, text: str, *, title: str
) -> uuid.UUID:
    """Task for the Application owner (an application of this Candidate only), else the hand-off role."""
    async with tenant_session(tenant_id) as db:
        owner = None
        if application_id is not None:
            row = (
                await db.execute(
                    select(_applications.c.owner_id).where(
                        _applications.c.id == application_id, _applications.c.candidate_id == candidate_id
                    )
                )
            ).first()
            if row is None:
                raise NotFound("Application not found")
            owner = row[0]
        assignee = await _active(db, owner) or await _fallback(db, tenant_id)
        entity = (
            ("application", application_id) if application_id is not None else ("candidate", candidate_id)
        )
        task_id = await _task(db, tenant_id, assignee, title, text, entity=entity, source="agent")
        emit(db, PortalCopilotHandoff(tenant_id, candidate_id, application_id, task_id))
        return task_id


__all__ = ["NOTE_MAX", "candidate_handoff", "message_account_manager", "message_vendor_manager"]
