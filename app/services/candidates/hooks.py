"""Registrations of the ``candidates`` domain (autodiscovered by ``core.hooks``; Epic 13).

The notification recipient directory for ``candidate`` recipients (workers, AD-27): primary email, full name
and the worker organization (the candidate itself).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import entity_refs
from app.services.notifications_service import RecipientInfo, register_recipient_directory


async def _candidate_directory(db: AsyncSession, candidate_id: uuid.UUID) -> RecipientInfo | None:
    found = await entity_refs.addressee(db, "candidate", candidate_id)
    if found is None:
        return None
    return RecipientInfo(found.address, found.full_name or "", ("worker", candidate_id))


register_recipient_directory("candidate", _candidate_directory)


# ---------------------------------------------------------------- Epic 36: semantic base source
def _register_semantic_source() -> None:
    from app.services.candidates import semantic_source

    semantic_source.register()


_register_semantic_source()
