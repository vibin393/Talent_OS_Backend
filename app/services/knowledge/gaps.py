# ruff: file-ignore[line-too-long]
"""FAQ gap report (Epic 62 A1 / B5): onboarding questions the assistant could not answer -- handed off, ungrounded or
marked incorrect by the asker.

Epic 78 (FR-404): questions are clustered by the non-reversible ``topic_label`` and, within a label, by the HMAC
``question_fingerprint`` (no plaintext grouping, no full-text index). A representative question of each cluster is
decrypted only for ``knowledge:manage`` holders; everyone else sees the label."""

import re
import uuid  # ruff: ignore[typing-only-standard-library-import]
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal
from app.models.knowledge import OnboardingQaMessage
from app.schemas.knowledge import GapRow
from app.services.knowledge import sealing

_PUNCT = re.compile(r"[^\w\s]")
_PERSON_REQUEST = "I would like to talk to my coordinator"


def normalise(question: str) -> str:
    return " ".join(_PUNCT.sub(" ", question.lower()).split())[:300]


def _outcome(msg: OnboardingQaMessage) -> str | None:
    if msg.feedback == "incorrect":
        return "incorrect"
    if msg.outcome in ("handed_off", "ungrounded"):
        return msg.outcome
    return None


def _can_decrypt(ctx: RequestContext) -> bool:
    p = ctx.principal
    return isinstance(p, Principal) and p.type == "user" and p.has("knowledge:manage")


async def gap_report(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    topic: str | None = None,
    limit: int = 200,
) -> list[GapRow]:
    rows = list(
        (
            await db.execute(
                select(OnboardingQaMessage)
                .order_by(OnboardingQaMessage.thread_id, OnboardingQaMessage.seq)
                .limit(20_000)
            )
        ).scalars()
    )
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    last_question: dict[uuid.UUID, OnboardingQaMessage] = {}
    for m in rows:
        if m.role == "asker":
            if m.question_fingerprint != sealing.fingerprint(m.tenant_id, _PERSON_REQUEST + "."):
                last_question[m.thread_id] = m
            continue
        outcome = _outcome(m)
        question = last_question.get(m.thread_id)
        if outcome is None or question is None:
            continue
        if topic and m.topic != topic:
            continue
        label = question.topic_label or (m.topic or "general")
        key = (label, question.question_fingerprint or str(question.id))
        g = groups.setdefault(
            key,
            {"rep": question, "label": label, "topic": m.topic, "count": 0, "outcome": outcome, "last": question.created_at, "threads": []},
        )
        g["count"] += 1
        g["topic"] = g["topic"] or m.topic
        if question.created_at > g["last"]:
            g["last"] = question.created_at
            g["outcome"] = outcome
        if m.thread_id not in g["threads"]:
            g["threads"].append(m.thread_id)
    decrypt = _can_decrypt(ctx)
    out: list[GapRow] = []
    for g in groups.values():
        text = (await sealing.text_of(db, g["rep"]))[:300] if decrypt else ""
        out.append(
            GapRow(
                question=text or g["label"],
                topic=g["topic"],
                topic_label=g["label"],
                times_asked=g["count"],
                outcome=g["outcome"],
                last_asked=g["last"] if isinstance(g["last"], datetime) else datetime.now(),
                thread_ids=g["threads"][:20],
            )
        )
    out.sort(key=lambda r: (-r.times_asked, r.last_asked), reverse=False)
    return out[:limit]


__all__ = ["gap_report", "normalise"]
