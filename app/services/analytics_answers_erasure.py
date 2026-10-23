# ruff: file-ignore[line-too-long]
"""Erasure handler ``analytics_answers.records`` (Epic 64, A8; FR-326).

For a candidate subject:

* answers produced by agent runs whose ``candidate_ids`` contain the candidate (or whose subject is the candidate):
  ``question`` -> "Redacted", ``assumptions`` -> ``[]``;
* any answer whose ``question`` / ``assumptions`` name the candidate (full name or human id): the same;
* any answer whose definition filter values equal the candidate's name, human id or id: those values -> ``"[redacted]"``
  and the answer is set ``needs_repair`` (definitions are otherwise kept: they hold keys and filter values).

Order **9** (deviation from the spec's 67): the handler must read the candidate's name before ``candidates.profile``
(order 10) anonymises it. It touches only ``analytics_answers``. Top-level module (``load_erasure_handlers``), so privacy
never imports Phase 4 code.
"""

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_runtime import AgentRun
from app.models.analytics_answers import AnalyticsAnswer
from app.models.candidates import Candidate
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

REDACTED_QUESTION = "Redacted"
REDACTED_VALUE = "[redacted]"


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _terms(db: AsyncSession, ids: list[uuid.UUID]) -> set[str]:
    terms = {str(i) for i in ids}
    rows = await db.execute(select(Candidate.full_name, Candidate.human_id).where(Candidate.id.in_(ids)))
    for name, human_id in rows.tuples():
        for t in (name, human_id):
            if t and len(t.strip()) >= 3:
                terms.add(t.strip())
    return terms


def _redact(node: Any, terms: set[str]) -> tuple[Any, bool]:
    lowered = {t.lower() for t in terms}
    if isinstance(node, dict):
        changed = False
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "value":
                new_v, c = _redact_value(v, lowered)
            else:
                new_v, c = _redact(v, terms)
            out[k] = new_v
            changed = changed or c
        return out, changed
    if isinstance(node, list):
        items = [_redact(v, terms) for v in node]
        return [i[0] for i in items], any(i[1] for i in items)
    return node, False


def _redact_value(value: Any, lowered: set[str]) -> tuple[Any, bool]:
    if isinstance(value, str):
        return (REDACTED_VALUE, True) if value.strip().lower() in lowered else (value, False)
    if isinstance(value, list):
        items = [_redact_value(v, lowered) for v in value]
        return [i[0] for i in items], any(i[1] for i in items)
    return value, False


def _mentions(texts: Iterable[str], terms: set[str]) -> bool:
    joined = " ".join(texts).lower()
    return any(t.lower() in joined for t in terms)


async def _run_ids(db: AsyncSession, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    rows = await db.execute(
        select(AgentRun.id).where(
            or_(
                (AgentRun.subject_type == "candidate") & AgentRun.subject_id.in_(ids),
                AgentRun.candidate_ids.overlap(ids),
            )
        )
    )
    return list(rows.scalars())


async def _affected(db: AsyncSession, ids: list[uuid.UUID], terms: set[str]) -> list[AnalyticsAnswer]:
    run_ids = await _run_ids(db, ids)
    likes = [f"%{_escape(t)}%" for t in terms]
    conds = [AnalyticsAnswer.run_id.in_(run_ids or [uuid.uuid4()])]
    for like in likes:
        conds.extend(
            [
                AnalyticsAnswer.question.ilike(like, escape="\\"),
                cast(AnalyticsAnswer.assumptions, String).ilike(like, escape="\\"),
                cast(AnalyticsAnswer.definition, String).ilike(like, escape="\\"),
            ]
        )
    rows = await db.execute(select(AnalyticsAnswer).where(or_(*conds)))
    return list(rows.scalars())


class AnalyticsAnswersHandler:
    name = "analytics_answers.records"
    order = 9
    tables = ("analytics_answers",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"analytics_answers": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        terms = await _terms(db, ids)
        run_ids = set(await _run_ids(db, ids))
        count = 0
        for answer in await _affected(db, ids, terms):
            touched = False
            if answer.run_id in run_ids or _mentions([answer.question, *(answer.assumptions or [])], terms):
                answer.question = REDACTED_QUESTION
                answer.assumptions = []
                touched = True
            new_def, changed = _redact(answer.definition, terms)
            if changed:
                answer.definition = new_def
                answer.status = "needs_repair"
                touched = True
            count += int(touched)
        await db.flush()
        return {"analytics_answers": count}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        ids = await subject_ids(db, ctx.candidate_id)
        rows = await _affected(db, ids, await _terms(db, ids))
        return {
            "analytics_answers": [
                {"question": a.question, "assumptions": a.assumptions, "created_at": a.created_at}
                for a in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        run_ids = await _run_ids(db, ids)
        left = (
            await db.execute(
                select(AnalyticsAnswer.id)
                .where(
                    AnalyticsAnswer.run_id.in_(run_ids or [uuid.uuid4()]),
                    AnalyticsAnswer.question != REDACTED_QUESTION,
                )
                .limit(1)
            )
        ).first()
        return ["analytics answers keep a question about the candidate"] if left else []


register_erasure_handler(AnalyticsAnswersHandler())
