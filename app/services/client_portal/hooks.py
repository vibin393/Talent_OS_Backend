"""Registrations of the Client Portal content epic (Epic 18; autodiscovered by ``core.hooks``).

* Notification types ``client_portal.demand_requested`` (Account Manager), ``client_portal.slots_proposed``,
  ``client_portal.approvals_pending`` and ``client_portal.compliance_pack_published`` (Client Users).
* Approvals: delegate resolver ``client_user`` -> ``client_delegations.acting_for`` (foundation R5), approver kind
  ``client_hiring_manager`` (the demand's hiring-manager contact's active Client User; none -> 409
  ``client_approver_unresolved``).
* Secure Link approvals: ``ApprovalStepActivated`` for a ``client_user`` step issues the ``client_approval`` link
  (after commit, Celery); ``ApprovalCompleted`` revokes the request's open links.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.events import subscribe
from app.events.approvals import ApprovalCompleted, ApprovalStepActivated
from app.models.demands import Demand
from app.models.external_auth import ClientUser
from app.services import client_delegations
from app.services import notifications_service as ns
from app.services.approvals import ResolvedApprover, register_approver_resolver, register_delegate_resolver


async def _payload_users(db: AsyncSession, payload: Mapping[str, Any]) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    raw = payload.get("owner_id")
    return {uuid.UUID(str(raw))} if raw else set()


async def _payload_client_users(db: AsyncSession, payload: Mapping[str, Any]) -> set[ns.Recipient]:  # ruff: ignore[unused-function-argument]
    return {ns.Recipient("client_user", uuid.UUID(str(u))) for u in payload.get("client_user_ids") or []}


def _render(title: str, link: str | None) -> Any:
    async def render(db: AsyncSession, payload: Mapping[str, Any]) -> ns.Content:  # ruff: ignore[unused-function-argument]
        return ns.Content(title=str(payload.get("title") or title)[:300], link_path=link)

    return render


def _register_notifications() -> None:
    types = {
        "client_portal.demand_requested": ns.NotificationType(
            "Client demand request", "Demands", "both", True, _payload_users, _render("New client demand request", None)
        ),
        "client_portal.slots_proposed": ns.NotificationType(
            "Client proposed interview slots", "Interviews", "both", False, _payload_users,
            _render("The client proposed interview slots", None),
        ),
        "client_portal.approvals_pending": ns.NotificationType(
            "Approvals pending (client)", "Approvals", "both", False, _payload_client_users,
            _render("Items are awaiting your approval", "/client/approvals"),
        ),
        "client_portal.compliance_pack_published": ns.NotificationType(
            "Compliance pack published", "Clients", "both", False, _payload_client_users,
            _render("A compliance pack was published", "/client/compliance"),
        ),
    }  # fmt: skip
    for key, definition in types.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


async def _resolve_hiring_manager(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    approver_ref: Mapping[str, Any],  # ruff: ignore[unused-function-argument]
    facts: Mapping[str, Any],
) -> ResolvedApprover:
    raw = facts.get("demand_id") or facts.get("subject_id")
    demand = (
        (await db.execute(select(Demand).where(Demand.id == uuid.UUID(str(raw))))).scalar_one_or_none()
        if raw
        else None
    )
    contact_id = demand.hiring_manager_contact_id if demand is not None else None
    user = (
        (
            await db.execute(
                select(ClientUser).where(
                    ClientUser.contact_id == contact_id,
                    ClientUser.client_id == demand.client_id,  # type: ignore[union-attr]
                    ClientUser.status == "active",
                )
            )
        ).scalar_one_or_none()
        if contact_id is not None
        else None
    )
    if user is None:
        raise ConflictError(
            "The demand's hiring manager has no active Client Portal access",
            code="client_approver_unresolved",
        )
    return ResolvedApprover("client_user", None, None, user.id, ("client", user.client_id), user.display_name)


async def _delegates(
    ctx: RequestContext, db: AsyncSession, principal_id: uuid.UUID, on_date: Any
) -> set[uuid.UUID]:
    return await client_delegations.delegate_resolver(ctx, db, principal_id, on_date)


def _on_step_activated(event: ApprovalStepActivated) -> None:
    from app.tasks import client_portal as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.revoke_approval_links, str(event.tenant_id), str(event.request_id))
    if event.approver_kind == "client_user" and event.approver_client_user_id is not None:
        enqueue(
            tasks.issue_approval_link,
            str(event.tenant_id),
            str(event.request_id),
            str(event.approver_client_user_id),
        )


def _on_completed(event: ApprovalCompleted) -> None:
    from app.tasks import client_portal as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.revoke_approval_links, str(event.tenant_id), str(event.request_id))


_registered = False


def register() -> None:
    global _registered  # process-wide wiring, once
    if _registered:
        return
    _registered = True
    _register_notifications()
    register_delegate_resolver("client_user", _delegates)
    register_approver_resolver("client_hiring_manager", _resolve_hiring_manager)
    subscribe(ApprovalStepActivated, _on_step_activated)
    subscribe(ApprovalCompleted, _on_completed)


register()
