# ruff: noqa: E501, D401
"""Facade of the ``scheduling_agent`` domain (Epic 53 contract layer, spec 0.12; filled by Epic 59). Other domains call
these, never the package internals.

* ``propose`` -- the frozen contract (Epic 54's Copilot wraps it).
* Epic 59 (additive): ``requests`` (start / read / cancel / reschedule requests / the run chain), ``offers`` (send, the
  Candidate's pick / counter / time zone, portal list), ``booking``, ``reminders``, ``history``, ``settings_api``,
  ``availability``.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services.scheduling_agent import (
    availability,
    booking,
    history,
    offers,
    reminders,
    requests,
    settings_api,
)
from app.services.scheduling_agent.service import propose


async def application_owner(
    ctx: RequestContext,  # noqa: ARG001 - AgentSpec.owner signature
    db: AsyncSession,
    subject: tuple[str, uuid.UUID] | None,
) -> uuid.UUID | None:
    """The Accountable Owner of a scheduling run: the Application owner (reminder runs pass the organiser)."""
    from app.models.pipeline import Application

    if subject is None or subject[0] != "application":
        return None
    app = await db.get(Application, subject[1])
    return app.owner_id if app is not None else None


# Epic 72 (D-18, additive): a calendar change asks for a reschedule (system actor; None when the agent is off).
request_reschedule_system = requests.request_reschedule_system

__all__ = [
    "application_owner",
    "availability",
    "booking",
    "history",
    "offers",
    "propose",
    "reminders",
    "request_reschedule_system",
    "requests",
    "settings_api",
]
