"""Candidate facts the privacy domain needs (Epic 12), exposed by the candidates domain (AD-1).

The privacy service never queries candidate tables itself: consent-flag maintenance, DSR subject lookup,
consent-request recipients and retention inactivity all go through these functions.
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidates import Candidate, CandidateEmail, CandidateResume


async def exists(db: AsyncSession, candidate_id: uuid.UUID) -> bool:
    return (await db.execute(select(Candidate.id).where(Candidate.id == candidate_id))).first() is not None


async def basics(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """``{id: {human_id, full_name, source, erased_at, ai_consent_state, primary_email}}``."""
    id_list = list(dict.fromkeys(ids))
    if not id_list:
        return {}
    rows = (await db.execute(select(Candidate).where(Candidate.id.in_(id_list)))).scalars().all()
    emails: dict[uuid.UUID, str] = {}
    for cid, email in (
        await db.execute(
            select(CandidateEmail.candidate_id, CandidateEmail.email)
            .where(CandidateEmail.candidate_id.in_(id_list))
            .order_by(CandidateEmail.is_primary.desc(), CandidateEmail.created_at)
        )
    ).all():
        emails.setdefault(cid, email)
    return {
        r.id: {
            "human_id": r.human_id,
            "full_name": r.full_name,
            "source": r.source,
            "erased_at": r.erased_at,
            "archived_at": r.archived_at,
            "ai_consent_state": r.ai_consent_state,
            "updated_at": r.updated_at,
            "primary_email": emails.get(r.id),
        }
        for r in rows
    }


async def set_ai_consent_state(db: AsyncSession, states: dict[uuid.UUID, str]) -> int:
    """Write ``ai_consent_state`` (ORM, so the change is audited). Returns the number of rows changed."""
    if not states:
        return 0
    from app.core.events import emit
    from app.events.candidates import AIConsentGranted, AIConsentWithdrawn

    changed = 0
    rows = (await db.execute(select(Candidate).where(Candidate.id.in_(list(states))))).scalars()
    for row in rows:
        old, new = row.ai_consent_state, states[row.id]
        if old != new:
            row.ai_consent_state = new
            changed += 1
            # Consumed by the search/matching domains (Epic 5): drop or rebuild AI-derived rows.
            if new == "withdrawn":
                emit(db, AIConsentWithdrawn(tenant_id=row.tenant_id, candidate_id=row.id))
            elif new in ("granted", "not_required") and old in ("withdrawn", "pending"):
                emit(db, AIConsentGranted(tenant_id=row.tenant_id, candidate_id=row.id))
    await db.flush()
    return changed


def ids_stmt(*, include_erased: bool = False) -> Select[tuple[uuid.UUID]]:
    stmt = select(Candidate.id).where(Candidate.merged_into_id.is_(None))
    if not include_erased:
        stmt = stmt.where(Candidate.erased_at.is_(None))
    return stmt


async def all_ids(db: AsyncSession, *, include_erased: bool = False) -> list[uuid.UUID]:
    return list((await db.execute(ids_stmt(include_erased=include_erased))).scalars())


async def ids_with_state(db: AsyncSession, state: str) -> list[uuid.UUID]:
    stmt = ids_stmt().where(Candidate.ai_consent_state == state)
    return list((await db.execute(stmt)).scalars())


async def count_with_state(db: AsyncSession, state: str) -> int:
    stmt = select(func.count()).select_from(Candidate).where(
        Candidate.ai_consent_state == state, Candidate.erased_at.is_(None), Candidate.merged_into_id.is_(None)
    )
    return int((await db.execute(stmt)).scalar_one())


async def updated_before(db: AsyncSession, cutoff: datetime) -> dict[uuid.UUID, datetime]:
    """Retention pre-filter: live, non-erased candidates whose record was last updated before ``cutoff``."""
    stmt = select(Candidate.id, Candidate.updated_at).where(
        Candidate.erased_at.is_(None), Candidate.merged_into_id.is_(None), Candidate.updated_at < cutoff
    )
    return dict((await db.execute(stmt)).tuples().all())


async def updated_at(db: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    if not ids:
        return {}
    stmt = select(Candidate.id, Candidate.updated_at).where(Candidate.id.in_(list(ids)))
    return dict((await db.execute(stmt)).tuples().all())


async def resume_ids(db: AsyncSession, candidate_id: uuid.UUID) -> list[uuid.UUID]:
    stmt = select(CandidateResume.id).where(CandidateResume.candidate_id == candidate_id)
    return list((await db.execute(stmt)).scalars())


async def touch(db: AsyncSession, candidate_id: uuid.UUID) -> None:
    """Bump ``updated_at`` (tests / imports use this to mark a candidate active again)."""
    await db.execute(update(Candidate).where(Candidate.id == candidate_id).values(updated_at=func.now()))
