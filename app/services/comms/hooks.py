"""Registrations of the comms reply-capture extension (Epic 33, FR-171; autodiscovered, AD-31):

* workflow trigger ``comms.email_reply_received`` (entity: the reply's candidate);
* notification type ``comms.reply_received`` -- the user whose mailbox email was answered.

SMS / WhatsApp registrations live in ``app.services.messaging.hooks`` (that package depends on the connector
framework, which ``app.services.comms`` may not import -- import contract ``p1-infra-below-p2``).
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.events.comms import EmailReplyReceived
from app.services import entity_refs
from app.services.notifications_service import (
    Content,
    NotificationType,
    register_event_source,
    register_notification_type,
)
from app.services.workflow.registry import Trigger, register_trigger


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def _sender(db: AsyncSession, payload: dict[str, Any]) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    user_id = _uuid(payload.get("sender_user_id"))
    return {user_id} if user_id else set()


async def _render_reply(db: AsyncSession, payload: dict[str, Any]) -> Content:
    for entity_type, key in (("candidate", "candidate_id"), ("contact", "contact_id")):
        entity_id = _uuid(payload.get(key))
        if entity_id is not None:
            label = await entity_refs.entity_label(db, entity_type, entity_id)
            return Content(
                title=f"{label or 'A recipient'} replied to your email",
                entity_type=entity_type,
                entity_id=entity_id,
                link_path=entity_refs.link_path(entity_type, entity_id),
            )
    message_id = _uuid(payload.get("email_message_id"))
    return Content(
        title="A reply to your email arrived",
        entity_type="email_message",
        entity_id=message_id,
        link_path=entity_refs.link_path("email_message", message_id),
    )


register_notification_type(
    "comms.reply_received",
    NotificationType("Reply to my email received", "Communication", "in_app", False, _sender, _render_reply),
)
register_event_source(EmailReplyReceived, "comms.reply_received")
register_trigger(
    "comms.email_reply_received",
    Trigger(
        "comms.email_reply_received",
        "Email reply received",
        "app.events.comms",
        "EmailReplyReceived",
        "candidate",
        "candidate_id",
    ),
)
