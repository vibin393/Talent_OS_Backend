# ruff: file-ignore[line-too-long]
"""Facade of the ``voice`` domain (Epic 58, Voice Screening; FR-293..FR-296). Routers, tools and the agent definition
call these, never the package internals.

* Sessions: ``request``, ``list_for_application``, ``get``, ``cancel``, ``transcript``, ``label_speakers``,
  ``complete_review`` (human only), ``application_owner`` (the agent's Accountable Owner).
* Consent page: ``consent_view``, ``consent_decide``. Uploads: ``upload_transcript``, ``upload_recording``.
  Playback: ``playback_url``. Recording Policy admin: ``policy``. Settings: ``get_settings``, ``update_settings``.
* Agent tool operations: ``ops``.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services.voice import agent_ops as ops
from app.services.voice import policy_admin as policy
from app.services.voice import uploads
from app.services.voice.consent import decide as consent_decide
from app.services.voice.consent import view as consent_view
from app.services.voice.playback import url as playback_url
from app.services.voice.provider_jobs import on_provider_event  # Epic 76 (additive, D-22)
from app.services.voice.service import get_settings_out as get_settings
from app.services.voice.service import update_settings
from app.services.voice.sessions import (
    cancel,
    complete_review,
    get,
    label_speakers,
    list_for_application,
    request,
    transcript,
)
from app.services.voice.uploads import upload_recording, upload_transcript


async def application_owner(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    subject: tuple[str, uuid.UUID] | None,
) -> uuid.UUID | None:
    """Return the Accountable Owner of a voice screening run: the Application owner."""
    from app.models.pipeline import Application

    if subject is None or subject[0] != "application":
        return None
    app = await db.get(Application, subject[1])
    return app.owner_id if app is not None else None


__all__ = [
    "application_owner",
    "cancel",
    "complete_review",
    "consent_decide",
    "consent_view",
    "get",
    "get_settings",
    "label_speakers",
    "list_for_application",
    "on_provider_event",
    "ops",
    "playback_url",
    "policy",
    "request",
    "transcript",
    "update_settings",
    "upload_recording",
    "upload_transcript",
    "uploads",
]
