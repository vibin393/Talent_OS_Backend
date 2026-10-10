"""Epic 33 erasure handler ``comms.channels`` (52, after ``activity.entity``; FR-68 / AD-34).

* ``inbound_emails`` filed under the candidate: deleted; their ``email_threads`` stop and drop the candidate
  from ``related`` (``activity.entity`` already blanked the ``email_reply`` / ``sms`` / ``whatsapp`` entries).
* ``channel_messages`` to / from the candidate: phone, body, variables and related records cleared.
* ``channel_opt_outs`` of the candidate: the phone number is cleared; the keyed ``phone_hash`` stays so the
  suppression survives erasure.
"""

import uuid
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comms import ChannelMessage, ChannelOptOut, EmailThread, InboundEmail
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"


def _refs(ids: list[uuid.UUID]) -> list[Any]:
    return [InboundEmail.related.contains([{"entity_type": "candidate", "entity_id": str(i)}]) for i in ids]


def _thread_refs(ids: list[uuid.UUID]) -> list[Any]:
    return [EmailThread.related.contains([{"entity_type": "candidate", "entity_id": str(i)}]) for i in ids]


def _messages(ids: list[uuid.UUID]) -> Any:
    return select(ChannelMessage).where(
        or_(
            ChannelMessage.candidate_id.in_(ids),
            (ChannelMessage.recipient_kind == "candidate") & ChannelMessage.recipient_id.in_(ids),
        )
    )


class CommsChannelsHandler:
    name = "comms.channels"
    order = 52
    tables = ("inbound_emails", "email_threads", "channel_messages", "channel_opt_outs")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        keys = {str(i) for i in ids}
        replies = (
            (await db.execute(delete(InboundEmail).where(or_(*_refs(ids))).returning(InboundEmail.id)))
            .scalars()
            .all()
        )
        threads = list((await db.execute(select(EmailThread).where(or_(*_thread_refs(ids))))).scalars())
        for t in threads:
            t.status = "stopped"
            t.related = [
                r
                for r in t.related or []
                if not (r.get("entity_type") == "candidate" and str(r.get("entity_id")) in keys)
            ]
        messages = list((await db.execute(_messages(ids))).scalars())
        for m in messages:
            m.to_e164, m.body_rendered, m.variables, m.related = None, ERASED, {}, []
        opt_outs = list(
            (await db.execute(select(ChannelOptOut).where(ChannelOptOut.candidate_id.in_(ids)))).scalars()
        )
        for o in opt_outs:
            o.phone_e164, o.reason = None, None
        await db.flush()
        return {
            "inbound_emails": len(replies),
            "email_threads": len(threads),
            "channel_messages": len(messages),
            "channel_opt_outs": len(opt_outs),
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        ids = [ctx.candidate_id]
        replies = (await db.execute(select(InboundEmail).where(or_(*_refs(ids))))).scalars()
        messages = (await db.execute(_messages(ids))).scalars()
        return {
            "email_replies": [
                {"subject": r.subject, "received_at": r.received_at, "body": r.body_text} for r in replies
            ],
            "messages": [
                {
                    "channel": m.channel,
                    "direction": m.direction,
                    "status": m.status,
                    "body": m.body_rendered,
                    "at": m.created_at,
                }
                for m in messages
            ],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        issues = [
            f"inbound email {r} remains"
            for r in (await db.execute(select(InboundEmail.id).where(or_(*_refs(ids))))).scalars()
        ]
        issues += [
            f"channel message {m.id} keeps a phone or body"
            for m in (await db.execute(_messages(ids))).scalars()
            if m.to_e164 or m.body_rendered != ERASED
        ]
        issues += [
            f"opt-out {o} keeps a phone"
            for o in (
                await db.execute(
                    select(ChannelOptOut.id).where(
                        ChannelOptOut.candidate_id.in_(ids), ChannelOptOut.phone_e164.is_not(None)
                    )
                )
            ).scalars()
        ]
        return issues


register_erasure_handler(CommsChannelsHandler())
