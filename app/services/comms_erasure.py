"""Story 22.9 (A10) erasure handler ``comms.emails`` (52; FR-68 / AD-34).

Logged emails about the candidate (``email_messages`` filed under the candidate via ``related`` or sent to a
recipient row linked to the candidate):

* ``email_messages``: subject and bodies -> ``[erased]``; ``reply_to`` cleared; the candidate dropped from
  ``related``. Delivery facts (status, timestamps, provider id) stay for the send log.
* ``email_recipients`` linked to the candidate: the address is replaced by a non-routable placeholder and the
  bounce reason cleared (the row keeps kind / status for delivery counts).

Rows in ``ctx.retained["email_messages"]`` (and their recipients) are kept untouched (retention class, e.g.
invoice dispatch). Only candidate subjects are handled; other subject kinds return zero counts.
"""

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comms import Email, EmailRecipient
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"
ERASED_DOMAIN = "erased.invalid"


def erased_address(recipient_id: uuid.UUID) -> str:
    return f"erased-{recipient_id.hex}@{ERASED_DOMAIN}"


def _recipients(ids: list[uuid.UUID]) -> Any:
    return select(EmailRecipient).where(
        EmailRecipient.entity_type == "candidate", EmailRecipient.entity_id.in_(ids)
    )


def _messages(ids: list[uuid.UUID]) -> Any:
    related = [Email.related.contains([{"entity_type": "candidate", "entity_id": str(i)}]) for i in ids]
    linked = select(EmailRecipient.message_id).where(
        EmailRecipient.entity_type == "candidate", EmailRecipient.entity_id.in_(ids)
    )
    return select(Email).where(or_(*related, Email.id.in_(linked)))


class CommsEmailsHandler:
    name = "comms.emails"
    order = 52
    tables = ("email_messages", "email_recipients")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"email_messages": 0, "email_recipients": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        keys = {str(i) for i in ids}
        keep = ctx.retained.get("email_messages", set())
        messages = [m for m in (await db.execute(_messages(ids))).scalars() if m.id not in keep]
        for m in messages:
            m.subject, m.body_html, m.body_text, m.reply_to = ERASED, ERASED, ERASED, None
            m.related = [
                r
                for r in m.related or []
                if not (r.get("entity_type") == "candidate" and str(r.get("entity_id")) in keys)
            ]
        recipients = [r for r in (await db.execute(_recipients(ids))).scalars() if r.message_id not in keep]
        for r in recipients:
            r.address, r.bounce_reason = erased_address(r.id), None
        await db.flush()
        return {"email_messages": len(messages), "email_recipients": len(recipients)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        messages = (await db.execute(_messages([ctx.candidate_id]))).scalars()
        return {
            "emails": [
                {"subject": m.subject, "status": m.status, "sent_at": m.sent_at, "body": m.body_text}
                for m in messages
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        keep = ctx.retained.get("email_messages", set())
        issues = [
            f"email {m.id} keeps a subject or body"
            for m in (await db.execute(_messages(ids))).scalars()
            if m.id not in keep and m.subject != ERASED
        ]
        issues += [
            f"email recipient {r.id} keeps an address"
            for r in (await db.execute(_recipients(ids))).scalars()
            if r.message_id not in keep and not str(r.address).endswith(f"@{ERASED_DOMAIN}")
        ]
        return issues


register_erasure_handler(CommsEmailsHandler())
