# ruff: noqa: E501, D400, D415
"""Validation of the assistant's writes (Epic 60, NFR-52):

* **Language** (``summary_language``): AI-written text (summaries, rationales, note suggestions) may describe what was
  said and how it relates to the Kit -- never tone, confidence of voice, emotion, personality, honesty, appearance or a
  protected trait. Quotes are the Candidate's own words and are not screened.
* **Evidence** (``invalid_evidence``): every quote must be a (whitespace- and case-normalised) substring of the
  referenced transcript segment.
"""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transcripts import TranscriptSegment
from app.services.interview_captures.common import invalid

LANGUAGE_BLOCKLIST = re.compile(
    r"\b(tone|tonality|personality|emotion\w*|emotional|nervous\w*|anxious|anxiety|confident|confidence of voice|"
    r"self[- ]assured|enthusias\w*|passionate|friendly|rude|polite|aggressive|accent|attitude|temperament|mood|"
    r"honest\w*|dishonest\w*|lying|liar|truthful\w*|sincer\w*|body language|appearance|voice|"
    r"age[ds]?|elderly|gender|male|female|man|woman|married|marital|pregnan\w*|religio\w*|caste|ethnic\w*|race|"
    r"racial|nationality|disab\w*|health|sexual\w*|family plans?|children|skin)\b",
    re.IGNORECASE,
)


def norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def check_language(*texts: str | None) -> None:
    for text in texts:
        if text and LANGUAGE_BLOCKLIST.search(text):
            raise invalid(
                "summary_language",
                "Describe only what was said against the Kit; never tone, emotion, personality, honesty, voice or personal traits",
                term=LANGUAGE_BLOCKLIST.search(text).group(0).lower(),  # type: ignore[union-attr]
            )


@dataclass(frozen=True)
class Segment:
    seq: int
    text: str
    start_ms: int | None
    role: str


async def segments(db: AsyncSession, transcript_id: uuid.UUID) -> dict[int, Segment]:
    rows = (
        await db.execute(select(TranscriptSegment).where(TranscriptSegment.transcript_id == transcript_id))
    ).scalars()
    return {r.seq: Segment(r.seq, r.text, r.start_ms, r.speaker_role) for r in rows}


def check_quote(segs: dict[int, Segment], seq: int, quote: str) -> Segment:
    seg = segs.get(seq)
    q = norm(quote)
    if seg is None or not q or q not in norm(seg.text):
        raise invalid(
            "invalid_evidence",
            "Each quote must be copied exactly from the referenced transcript segment",
            segment_seq=seq,
        )
    return seg


def check_any_quote(segs: dict[int, Segment], seqs: list[int], quote: str) -> Segment:
    for seq in seqs:
        try:
            return check_quote(segs, seq, quote)
        except Exception:
            continue
    raise invalid("invalid_evidence", "The response quote must come from the referenced transcript segments")


__all__ = [
    "LANGUAGE_BLOCKLIST",
    "Segment",
    "check_any_quote",
    "check_language",
    "check_quote",
    "norm",
    "segments",
]
