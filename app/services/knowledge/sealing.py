# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Onboarding Q&A encryption at rest (Epic 78, story 78.4, FR-404, AD-90).

The only module of ``knowledge`` that imports ``vault_service`` (listed ``p5-below`` exception, D-22). Message text and
feedback notes are sealed under the Tenant DEK (``vault_service.seal_field``; AAD = ``onboarding_qa_messages.<col>:<id>``
so a blob copied to another row or tenant does not open). Decryption happens only here, for the readers allowed by
the spec (the asker's thread, the coordinator view, the gap report under ``knowledge:manage``, the agent run context and
the erasure export). Message text never reaches a log line or an error.

* ``seal_message`` / ``open_message`` / ``seal_note`` / ``open_note``; ``open_messages`` fills the transient
  ``plain_text`` / ``plain_feedback_note`` of loaded rows.
* ``fingerprint`` -- per-tenant HMAC of the normalised question (non-reversible); ``topic_label`` -- a scrubbed
  generic label (emails, phones, digits and capitalised tokens removed) used by list views and the gap report.
* ``backfill`` / ``verify`` -- the resumable batched sealing of rows written before ``512_02`` (raw SQL: the plaintext
  columns exist only between ``512_02`` and ``512_03``; afterwards both are no-ops that report zero plaintext rows).
"""

import dataclasses
import hmac
import random
import re
import uuid

from loguru import logger
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encrypted import blind_index
from app.core.errors import ValidationFailed
from app.models.knowledge import OnboardingQaMessage
from app.services import vault_service
from app.services.knowledge.common import MAX_ASKER_TEXT

CTX_TEXT = "onboarding_qa_messages.text:{id}"
CTX_NOTE = "onboarding_qa_messages.feedback_note:{id}"
MAX_NOTE = 500
TOPIC_LABEL_MAX = 60
VERIFY_SAMPLE = 0.01

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_DIGITS = re.compile(r"\S*\d\S*")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
_PUNCT = re.compile(r"[^\w\s]")
_STOP = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "then", "so", "to", "of", "in", "on", "at", "by", "for", "with", "from", "about", "as", "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did", "have", "has", "had", "i", "me", "my", "mine", "we", "our", "you", "your", "he", "she", "it", "they", "them", "their", "this", "that", "these", "those", "what", "when", "where", "which", "who", "whom", "why", "how", "can", "could", "should", "would", "will", "shall", "may", "might", "must", "not", "no", "yes", "please", "hi", "hello", "thanks", "thank", "there", "here", "get", "got", "any", "some", "much", "many", "more", "most", "also", "just", "still", "very", "really", "need", "want", "know", "tell", "let", "like"]
)


@dataclasses.dataclass(frozen=True, slots=True)
class BackfillResult:
    sealed: int
    skipped: int
    failed: int
    last_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class VerifyResult:
    plaintext_rows: int
    undecryptable_rows: int


# ================================================================ seal / open
async def seal_message(db: AsyncSession, tenant_id: uuid.UUID, message_id: uuid.UUID, text: str) -> bytes:
    if len(text) > MAX_ASKER_TEXT:
        raise ValidationFailed(f"Messages are at most {MAX_ASKER_TEXT} characters", code="message_too_long")
    return await vault_service.seal_field(db, tenant_id, CTX_TEXT.format(id=message_id), text)


async def open_message(db: AsyncSession, tenant_id: uuid.UUID, message_id: uuid.UUID, blob: bytes) -> str:
    return await vault_service.open_field(db, tenant_id, CTX_TEXT.format(id=message_id), bytes(blob))


async def seal_note(db: AsyncSession, tenant_id: uuid.UUID, message_id: uuid.UUID, note: str) -> bytes:
    return await vault_service.seal_field(db, tenant_id, CTX_NOTE.format(id=message_id), note[:MAX_NOTE])


async def open_note(db: AsyncSession, tenant_id: uuid.UUID, message_id: uuid.UUID, blob: bytes) -> str:
    return await vault_service.open_field(db, tenant_id, CTX_NOTE.format(id=message_id), bytes(blob))


async def open_messages(db: AsyncSession, msgs: list[OnboardingQaMessage], *, notes: bool = False) -> list[OnboardingQaMessage]:
    """Decrypt into the transient ``plain_text`` (and ``plain_feedback_note``); an undecryptable row reads as ``""``."""
    for m in msgs:
        if m.plain_text is not None:
            continue
        try:
            m.plain_text = await open_message(db, m.tenant_id, m.id, m.text_sealed)
        except Exception:
            logger.warning("onboarding Q&A message {} could not be decrypted", m.id)
            m.plain_text = ""
        if notes and m.feedback_note_sealed is not None:
            try:
                m.plain_feedback_note = await open_note(db, m.tenant_id, m.id, m.feedback_note_sealed)
            except Exception:
                m.plain_feedback_note = ""
    return msgs


async def text_of(db: AsyncSession, m: OnboardingQaMessage) -> str:
    await open_messages(db, [m])
    return m.plain_text or ""


# ================================================================ fingerprint / topic label
def normalise(question: str) -> str:
    return " ".join(_PUNCT.sub(" ", (question or "").lower()).split())[:300]


def fingerprint(tenant_id: uuid.UUID, text: str) -> str:
    return blind_index(tenant_id, normalise(text) or "-")


def scrub(text: str) -> list[str]:
    """Generic keywords of a question: emails, URLs, anything with a digit and capitalised (name-like) tokens removed."""
    cleaned = _DIGITS.sub(" ", _URL.sub(" ", _EMAIL.sub(" ", text or "")))
    words: list[str] = []
    for m in _WORD.finditer(cleaned):
        w = m.group(0)
        before = cleaned[: m.start()].rstrip()
        sentence_start = not before or before[-1] in ".?!"
        if w[0].isupper() and not sentence_start and not (w.isupper() and len(w) <= 4):
            continue  # capitalised mid-sentence token: possibly a name or place
        low = w.lower()
        if low in _STOP or low in words:
            continue
        words.append(low)
    return words


def topic_label(topic: str | None, text: str) -> str:
    """``"{topic}: {top 3 keywords}"`` after scrubbing; just the topic when nothing generic is left."""
    prefix = (topic or "general")[:12]
    keywords = scrub(text)[:3]
    label = f"{prefix}: {' '.join(keywords)}" if keywords else prefix
    return label[:TOPIC_LABEL_MAX]


def relabel(label: str | None, topic: str | None) -> str | None:
    """Swap the topic prefix of a stored label once the answer's topic is known (keywords unchanged)."""
    if not label or not topic:
        return label
    _, _, rest = label.partition(": ")
    return (f"{topic}: {rest}" if rest else topic)[:TOPIC_LABEL_MAX]


# ================================================================ backfill / verify (between 512_02 and 512_03)
async def _plaintext_columns(db: AsyncSession) -> bool:
    found = (
        await db.execute(
            sql_text(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = 'onboarding_qa_messages' "
                "AND column_name IN ('text', 'feedback_note') AND table_schema = current_schema()"
            )
        )
    ).scalar_one()
    return int(found or 0) == 2


async def backfill(
    db: AsyncSession, tenant_id: uuid.UUID, *, batch: int = 500, resume_after: uuid.UUID | None = None
) -> BackfillResult:
    """Seal one batch of plaintext rows (ids after ``resume_after``). Every value is decrypted back and compared before
    its plaintext is NULLed; the first failure stops the run for the tenant (plaintext kept)."""
    if not await _plaintext_columns(db):
        return BackfillResult(0, 0, 0, None)
    rows = (
        await db.execute(
            sql_text(
                "SELECT id, text, feedback_note, topic, role FROM onboarding_qa_messages WHERE tenant_id = :tid "
                "AND (text IS NOT NULL OR feedback_note IS NOT NULL) AND (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid)) "
                "ORDER BY id LIMIT :n"
            ),
            {"tid": tenant_id, "after": str(resume_after) if resume_after else None, "n": batch},
        )
    ).all()
    sealed = skipped = 0
    last: uuid.UUID | None = resume_after
    for message_id, text, note, topic, role in rows:
        mid = uuid.UUID(str(message_id))
        try:
            params: dict[str, object] = {"id": mid, "tid": tenant_id}
            sets: list[str] = []
            if text is not None:
                blob = await vault_service.seal_field(db, tenant_id, CTX_TEXT.format(id=mid), str(text))
                if not hmac.compare_digest(await open_message(db, tenant_id, mid, blob), str(text)):
                    raise ValueError("round trip mismatch")
                sets += ["text_sealed = :tb", "text = NULL"]
                params["tb"] = blob
                if role == "asker":
                    sets += ["question_fingerprint = COALESCE(question_fingerprint, :fp)", "topic_label = COALESCE(topic_label, :tl)"]
                    params["fp"], params["tl"] = fingerprint(tenant_id, str(text)), topic_label(topic, str(text))
            if note is not None:
                nblob = await vault_service.seal_field(db, tenant_id, CTX_NOTE.format(id=mid), str(note))
                if not hmac.compare_digest(await open_note(db, tenant_id, mid, nblob), str(note)):
                    raise ValueError("round trip mismatch")
                sets += ["feedback_note_sealed = :nb", "feedback_note = NULL"]
                params["nb"] = nblob
            if not sets:
                skipped += 1
                continue
            await db.execute(sql_text(f"UPDATE onboarding_qa_messages SET {', '.join(sets)} WHERE id = :id AND tenant_id = :tid"), params)
            sealed += 1
            last = mid
        except Exception:
            logger.error("qa_sealing_failed tenant={} message={} (plaintext kept)", tenant_id, mid)
            return BackfillResult(sealed, skipped, 1, last)
    return BackfillResult(sealed, skipped, 0, last)


async def verify(db: AsyncSession, tenant_id: uuid.UUID, *, recent: list[uuid.UUID] | None = None) -> VerifyResult:
    """Plaintext rows left and undecryptable rows among a 1 % sample plus every id in ``recent``."""
    plaintext = 0
    if await _plaintext_columns(db):
        plaintext = int(
            (
                await db.execute(
                    sql_text(
                        "SELECT count(*) FROM onboarding_qa_messages WHERE tenant_id = :tid AND (text IS NOT NULL OR feedback_note IS NOT NULL)"
                    ),
                    {"tid": tenant_id},
                )
            ).scalar_one()
            or 0
        )
    rows = (
        await db.execute(
            sql_text("SELECT id, text_sealed, feedback_note_sealed FROM onboarding_qa_messages WHERE tenant_id = :tid AND text_sealed IS NOT NULL"),
            {"tid": tenant_id},
        )
    ).all()
    wanted = set(recent or [])
    bad = 0
    for message_id, blob, note_blob in rows:
        mid = uuid.UUID(str(message_id))
        if mid not in wanted and random.random() > VERIFY_SAMPLE:
            continue
        try:
            await open_message(db, tenant_id, mid, blob)
            if note_blob is not None:
                await open_note(db, tenant_id, mid, note_blob)
        except Exception:
            bad += 1
    return VerifyResult(plaintext, bad)


__all__ = [
    "CTX_NOTE",
    "CTX_TEXT",
    "BackfillResult",
    "VerifyResult",
    "backfill",
    "fingerprint",
    "normalise",
    "open_message",
    "open_messages",
    "open_note",
    "relabel",
    "scrub",
    "seal_message",
    "seal_note",
    "text_of",
    "topic_label",
    "verify",
]
