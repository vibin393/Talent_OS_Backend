"""Registrations of SMS / WhatsApp messaging (Epic 33; autodiscovered, AD-31):

* the send gate of the ``sms`` / ``whatsapp`` adapters and their webhook handlers;
* workflow actions ``comms.send_sms`` / ``comms.send_whatsapp`` (actor ``workflow``, ``origin=workflow``, the
  same refusals as the API) and the trigger ``comms.channel_inbound_received``;
* notification type ``comms.connector_failing`` (messaging admins, when a message fails after every retry);
* ``ConnectorJobFailed`` for ``channel_message`` jobs -> the message becomes ``failed``.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.events import subscribe
from app.events.comms import ChannelInboundReceived, ChannelMessageStatusChanged
from app.events.connectors import ConnectorJobFailed
from app.services.connectors.registry import register_webhook_handler
from app.services.connectors.sms.channel import register_send_gate
from app.services.messaging import inbound, sending
from app.services.notifications_service import (
    Content,
    NotificationType,
    register_event_source,
    register_notification_type,
)
from app.services.workflow.actions import ActionContext, ActionDef, ActionError, register_action
from app.services.workflow.registry import Trigger, register_trigger

register_send_gate(sending.MessagingGate())
register_webhook_handler("sms", inbound.handle_webhook)
register_webhook_handler("whatsapp", inbound.handle_webhook)


# ================================================================ workflow actions
class SendChannelParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: uuid.UUID
    to: Literal["candidate", "worker", "contact"] = "candidate"


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def _recipient(ac: ActionContext) -> sending.RecipientRef:
    params: SendChannelParams = ac.params
    if params.to in ("candidate", "worker"):
        cid = ac.entity_id if ac.entity_type == "candidate" else _uuid(ac.snapshot.get("candidate_id"))
        if cid is None:
            raise ActionError("candidate_unresolved", "The trigger has no candidate")
        return sending.RecipientRef(entity_type="candidate", entity_id=cid)
    contact = ac.entity_id if ac.entity_type == "contact" else _uuid(ac.snapshot.get("contact_id"))
    if contact is None and (client_id := _uuid(ac.snapshot.get("client_id"))) is not None:
        from app.services import entity_refs

        contact = await entity_refs.primary_contact_id(ac.db, client_id)
    if contact is None:
        raise ActionError("contact_unresolved", "The trigger has no contact")
    return sending.RecipientRef(entity_type="contact", entity_id=contact)


def _context(ac: ActionContext) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for key in ("demand_id", "client_id", "interview_id", "offer_id", "task_id"):
        if ac.snapshot.get(key):
            refs[key] = str(ac.snapshot[key])
    if ac.entity_type == "demand":
        refs["demand_id"] = str(ac.entity_id)
    return refs


def _action(channel: str) -> ActionDef:
    label = sending.LABEL[channel]

    async def run(ac: ActionContext) -> str:
        ref = await _recipient(ac)
        try:
            result = await sending.send(
                ac.ctx,
                ac.db,
                channel,
                ac.params.template_id,
                [ref],
                _context(ac),
                origin="workflow",
                workflow_execution_id=ac.execution_id,
            )
        except DomainError as exc:
            raise ActionError(exc.code, exc.message) from exc
        (item,) = result.results
        if item.status == "refused":
            raise ActionError(item.reason or "refused", f"{label} refused: {item.reason}")
        return f"Queued a {label} message to the {ac.params.to}"

    async def dry(ac: ActionContext) -> str:
        ref = await _recipient(ac)
        try:
            template = await sending._template_for(ac.db, channel, ac.params.template_id)
        except DomainError as exc:
            raise ActionError(exc.code, exc.message) from exc
        ev = await sending._evaluate(ac.ctx, ac.db, channel, template, ref, _context(ac), check_access=False)
        if ev.reason is not None:
            raise ActionError(ev.reason, f"{label} would be refused: {ev.reason}")
        return f"Would queue a {label} message to the {ac.params.to}"

    def validate(params: Any, entity_type: str, path: str) -> None:  # ruff: ignore[unused-function-argument]
        return None

    return ActionDef(f"Send {label} (template)", SendChannelParams, validate, run, dry)


register_action("comms.send_sms", _action("sms"))
register_action("comms.send_whatsapp", _action("whatsapp"))

register_trigger(
    "comms.channel_inbound_received",
    Trigger(
        "comms.channel_inbound_received",
        "SMS / WhatsApp reply received",
        "app.events.comms",
        "ChannelInboundReceived",
        "candidate",
        "candidate_id",
        matches=lambda event: bool(getattr(event, "matched", False)),
    ),
)


# ================================================================ notifications
async def _messaging_admins(db: AsyncSession, payload: dict[str, Any]) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    from app.services import identity_bridge

    return {row["id"] for row in await identity_bridge.users_with_permission(db, "messaging:configure")}


async def _render_failing(db: AsyncSession, payload: dict[str, Any]) -> Content:  # ruff: ignore[unused-function-argument]
    channel = sending.LABEL.get(str(payload.get("channel")), "Messaging")
    return Content(
        title=f"{channel} messages are failing: the provider could not be reached after every retry",
        link_path="/admin/messaging",
    )


register_notification_type(
    "comms.connector_failing",
    NotificationType(
        "Messaging provider failing", "Communication", "both", False, _messaging_admins, _render_failing
    ),
)
register_event_source(
    ChannelMessageStatusChanged,
    lambda p: "comms.connector_failing"
    if p.get("status") == "failed" and p.get("error_code") == "provider_unavailable"
    else None,
)


def _on_job_failed(event: ConnectorJobFailed) -> None:
    if event.subject_kind != "channel_message" or event.category not in ("sms", "whatsapp"):
        return
    from app.tasks import messaging as messaging_tasks
    from app.tasks.runtime import enqueue

    enqueue(messaging_tasks.job_failed, str(event.tenant_id), str(event.subject_id))


subscribe(ConnectorJobFailed, _on_job_failed)

__all__ = ["ChannelInboundReceived", "SendChannelParams"]
