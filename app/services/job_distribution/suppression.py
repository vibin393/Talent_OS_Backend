# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Board ingest suppression (Epic 70, spec A4 step 3): HMAC-SHA256 hashes of erased, consent-withdrawn and
Do-Not-Contact emails / phones, so a board applicant who asked to be forgotten is never re-created. Holds no
reversible personal data. Key: HMAC of ``core.security.key_material("identity")`` with the label
``board_suppression:<tenant>`` (the spec's dedicated purpose does not exist in ``KeyPurpose``; recorded)."""

import hashlib
import hmac
import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import key_material
from app.models.job_distribution import BoardIngestSuppression
from app.services.job_distribution.common import tenant_of


def _key(tenant_id: uuid.UUID) -> bytes:
    return hmac.new(key_material("identity").encode(), f"board_suppression:{tenant_id}".encode(), hashlib.sha256).digest()


def normalise_email(email: str | None) -> str | None:
    value = (email or "").strip().lower()
    return value or None


def normalise_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    from app.services.candidates.common import normalize_phone

    return normalize_phone(phone) or None


def identifier_hash(tenant_id: uuid.UUID, value: str) -> str:
    return hmac.new(_key(tenant_id), value.encode(), hashlib.sha256).hexdigest()


def hashes(tenant_id: uuid.UUID, email: str | None, phone: str | None) -> list[tuple[str, str]]:
    out = []
    if (e := normalise_email(email)) is not None:
        out.append(("email", identifier_hash(tenant_id, e)))
    if (p := normalise_phone(phone)) is not None:
        out.append(("phone", identifier_hash(tenant_id, p)))
    return out


async def add(db: AsyncSession, emails: Iterable[str], phones: Iterable[str], reason: str) -> int:
    tid = tenant_of(db)
    rows = [h for e in emails for h in hashes(tid, e, None)] + [h for p in phones for h in hashes(tid, None, p)]
    for kind, digest in rows:
        await db.execute(
            pg_insert(BoardIngestSuppression)
            .values(id=uuid.uuid4(), tenant_id=tid, identifier_hash=digest, kind=kind, reason=reason)
            .on_conflict_do_nothing(constraint="uq_board_ingest_suppressions_hash")
        )
    return len(rows)


async def match(db: AsyncSession, email: str | None, phone: str | None) -> str | None:
    """The suppression reason of this applicant, or None."""
    digests = [d for _, d in hashes(tenant_of(db), email, phone)]
    if not digests:
        return None
    return (
        await db.execute(select(BoardIngestSuppression.reason).where(BoardIngestSuppression.identifier_hash.in_(digests)).limit(1))
    ).scalar_one_or_none()


async def add_candidate(db: AsyncSession, candidate_id: uuid.UUID, reason: str) -> int:
    from app.models.candidates import CandidateEmail, CandidatePhone

    emails = [str(e) for e in (await db.execute(select(CandidateEmail.email).where(CandidateEmail.candidate_id == candidate_id))).scalars()]
    phones = [str(p) for p in (await db.execute(select(CandidatePhone.phone_e164).where(CandidatePhone.candidate_id == candidate_id))).scalars()]
    return await add(db, emails, phones, reason)
