# ruff: noqa: E501
"""Outbound send policy (Epic 53, story 53.8, FR-274, NFR-45). Runs before every egress tool call.

* Every recipient must be an existing record the run legitimately reached: the run subject, a record surfaced by an
  earlier tool (``related_refs``) or a run candidate -- never an id or address that only appeared in untrusted text.
  Access-checked kinds must also be readable by the Effective Rights. Otherwise -> ``policy_violation``
  (``recipient_not_on_record``). Args carrying a raw email address -> ``policy_violation`` (``address_in_args``).
* The channel must be allowed by the agent config (or the AgentSpec default; empty = all).
* Grounded answers need a conversation the person started or accepted (owner checker registered with
  ``register_grounded_conversation_checker``) and at least one citation, else ``policy_violation`` (``grounding_invalid``).
* ``sanitize.outbound`` on the rendered text: stripped links make the send untemplated (-> suggest).
The P2/P3 channel governance (consent, opt-outs, caps, quiet hours, DLT, WhatsApp session) runs inside the messaging
services afterwards.
"""

import dataclasses
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic.types import SendIntent
from app.core.context import RequestContext
from app.core.registry import Registry
from app.services import entity_refs
from app.services.agent_runtime import sanitize

GroundedChecker = Callable[[RequestContext, AsyncSession, uuid.UUID], Awaitable[bool]]
_grounded: Registry[str, GroundedChecker] = Registry("grounded conversation checker", allow_replace=True)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_ACCESS_KIND = {"candidate": "candidate", "contact": "contact", "worker": "placement"}


def register_grounded_conversation_checker(kind: str, fn: GroundedChecker) -> None:
    """Owners (57 ``screening``, 62 ``onboarding_qa``) register ``(ctx, db, conversation_id) -> started_or_accepted``."""
    _grounded.register(kind, fn)


@dataclasses.dataclass(frozen=True, slots=True)
class SendDecision:
    ok: bool
    intent: SendIntent
    violation: str | None = None
    grounded_ok: bool = False


def _scan_addresses(node: Any) -> bool:
    if isinstance(node, str):
        return bool(_EMAIL_RE.match(node.strip()))
    if isinstance(node, Mapping):
        return any(_scan_addresses(v) for v in node.values())
    if isinstance(node, list | tuple):
        return any(_scan_addresses(v) for v in node)
    return False


async def check(
    ctx: RequestContext,
    db: AsyncSession,
    intent: SendIntent,
    *,
    args: Mapping[str, Any],
    subject: tuple[str, uuid.UUID] | None,
    related_refs: Sequence[Sequence[str]],
    candidate_ids: Sequence[uuid.UUID],
    allowed_channels: Sequence[str],
    templated_required: bool = True,
) -> SendDecision:
    from app.services.agent_runtime.principals import scope_context

    owner = getattr(ctx, "on_behalf_of", None)
    scoped = scope_context(ctx, owner) if owner is not None else ctx
    if _scan_addresses(dict(args)):
        return SendDecision(False, intent, "address_in_args")
    known: set[str] = {str(ref[1]) for ref in related_refs if len(ref) == 2}
    known.update(str(c) for c in candidate_ids)
    if subject is not None:
        known.add(str(subject[1]))
    for recipient in intent.recipients:
        if str(recipient.id) not in known:
            return SendDecision(False, intent, "recipient_not_on_record")
        if recipient.kind == "user":
            if recipient.id not in await entity_refs.active_user_ids(db, [recipient.id]):
                return SendDecision(False, intent, "recipient_not_on_record")
        elif (kind := _ACCESS_KIND.get(recipient.kind)) is not None and entity_refs.is_access_checked(kind):
            if not await entity_refs.can_read(scoped, db, kind, recipient.id):
                return SendDecision(False, intent, "recipient_not_on_record")
        if allowed_channels and recipient.channel not in allowed_channels:
            return SendDecision(False, intent, "channel_not_allowed")
    grounded_ok = False
    if intent.grounded:
        checker = None
        if intent.conversation_id is not None:
            for kind_checker in _grounded.view().values():
                if await kind_checker(ctx, db, intent.conversation_id):
                    checker = kind_checker
                    break
        if checker is None or not intent.citations:
            return SendDecision(False, intent, "grounding_invalid")
        grounded_ok = True
    clean = sanitize.outbound(intent.text_for_checks)
    if clean.stripped_links and intent.templated and templated_required:
        intent = dataclasses.replace(intent, templated=False, text_for_checks=clean.text)
    return SendDecision(True, intent, None, grounded_ok)


__all__ = ["GroundedChecker", "SendDecision", "check", "register_grounded_conversation_checker"]
