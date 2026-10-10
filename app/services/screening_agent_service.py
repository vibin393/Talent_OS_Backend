# ruff: noqa: E501, D401, ARG001
"""Facade of the ``screening_agent`` domain (Epic 53 contract layer, spec 0.12; filled by Epic 57). Other domains call
these, never the package internals.

* ``extract_answers`` -- the frozen contract (used by 57 and Epic 58 voice screening).
* Epic 58 additions: ``resolve_for_application``, ``list_answers``, ``decide_answer``, ``complete_review``,
  ``erase_voice_answers``.
* Epic 65 (coordinator C-16): ``pause``.
* Agent tools / routers: ``ops`` (tool operations), ``conversations``, ``questionnaires``, ``review``, ``portal_chat``,
  ``agent_settings``, ``reminders``, ``inbound``, ``application_owner``.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services.screening_agent import agent_ops as ops
from app.services.screening_agent import (
    agent_settings,
    conversations,
    grounding,
    inbound,
    portal_chat,
    questionnaires,
    reminders,
    review,
)
from app.services.screening_agent.conversations import pause
from app.services.screening_agent.questionnaires import resolve_for_application
from app.services.screening_agent.review import complete as complete_review
from app.services.screening_agent.review import decide_answer, erase_voice_answers, list_answers
from app.services.screening_agent.service import extract_answers


async def application_owner(
    ctx: RequestContext, db: AsyncSession, subject: tuple[str, uuid.UUID] | None
) -> uuid.UUID | None:
    """The Accountable Owner of a screening run: the Application owner."""
    from app.models.pipeline import Application

    if subject is None or subject[0] != "application":
        return None
    app = await db.get(Application, subject[1])
    return app.owner_id if app is not None else None


__all__ = [
    "agent_settings",
    "application_owner",
    "complete_review",
    "conversations",
    "decide_answer",
    "erase_voice_answers",
    "extract_answers",
    "grounding",
    "inbound",
    "list_answers",
    "ops",
    "pause",
    "portal_chat",
    "questionnaires",
    "reminders",
    "resolve_for_application",
    "review",
]
