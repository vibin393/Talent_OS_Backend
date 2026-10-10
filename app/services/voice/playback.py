# ruff: noqa: E501
"""Playback (Epic 58, story 58.5; FR-296): the recording is downloaded through ``files_service`` only by people with
``screening:conduct`` who can read the Application, and only while ``recording_file_id`` is set (a withdrawn recording
is gone). The UI appends ``#t=<seconds>`` to start at an answer."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.permissions import Principal
from app.models.voice import VoiceSession
from app.schemas.voice import PlaybackOut
from app.services.voice import common, sessions

EXPIRES_IN = 300


async def can_access(ctx: RequestContext, db: AsyncSession, session_id: uuid.UUID, write: bool) -> bool:
    """``files_service`` access checker for files owned by ``voice_session`` records."""
    if write:
        return False
    principal = ctx.principal
    if not isinstance(principal, Principal) or not principal.has(common.CONDUCT):
        return False
    row = await db.get(VoiceSession, session_id)
    if row is None or row.recording_file_id is None or row.application_id is None:
        return False
    try:
        await sessions.readable_application(ctx, db, row.application_id)
    except NotFound:
        return False
    return True


async def url(ctx: RequestContext, db: AsyncSession, session_id: uuid.UUID) -> PlaybackOut:
    row = await sessions.readable(ctx, db, session_id)
    if row.recording_file_id is None:
        raise NotFound("No recording")
    return PlaybackOut(
        url=f"/api/v1/files/{row.recording_file_id}/download",
        expires_in=EXPIRES_IN,
        media_type=row.recording_media_type,
    )


__all__ = ["EXPIRES_IN", "can_access", "url"]
