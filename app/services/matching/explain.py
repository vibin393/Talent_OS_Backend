"""Match explanations (Epic 5; AD-8: the LLM explains, it never scores).

``explain_matches(ctx, db, demand_id)`` takes the top ``explain_top_n`` non-dismissed matches whose
``explanation_input_hash`` differs from ``input_hash`` and asks the gateway (``match_explain.v1``, subject =
the candidate, so consent / toggle / budget apply) for a <= 280-character text citing evidence ids. Inputs
are structured evidence only (compensation evidence and resume text are never sent). The service writes
**only** the ``explanation*`` columns; citing an unknown evidence id drops the explanation (``failed``).
Disabled / no consent / budget / not configured -> ``skipped``; provider or validation errors -> ``failed``.
"""

import json
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.matching import Match
from app.schemas.matching import MatchExplanation
from app.services.demands import matching_port as demands
from app.services.matching.settings import effective

SKIP_CODES = {"feature_disabled", "consent_missing", "budget_exceeded", "not_configured"}
FEATURE = "matching"
PROMPT = "match_explain.v1"


def _demand_summary(profile: Any) -> str:
    skills = ", ".join(f"{s.label}{' (mandatory)' if s.mandatory else ''}" for s in profile.skills[:20])
    parts = [f"Title: {profile.title}"]
    if skills:
        parts.append(f"Skills: {skills}")
    if profile.experience_min is not None or profile.experience_max is not None:
        parts.append(f"Experience: {profile.experience_min or 0}-{profile.experience_max or '+'} years")
    return "\n".join(parts)


def prompt_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evidence sent to the model: never compensation items."""
    return [{"id": e["id"], "kind": e["kind"], "text": e["text"]} for e in evidence if not e.get("comp")]


async def explain_one(ctx: RequestContext, db: AsyncSession, match: Any, demand_summary: str) -> str:
    """Explain one match row (``match`` = a row with id, candidate_id, sub_scores, evidence, input_hash)."""
    from app.services import ai

    evidence = prompt_evidence(list(match.evidence or []))
    result = await ai.run(
        ctx,
        db,
        FEATURE,
        PROMPT,
        inputs={"sub_scores": json.dumps(match.sub_scores, sort_keys=True)},
        untrusted={"demand_summary": demand_summary, "evidence": json.dumps(evidence, ensure_ascii=False)},
        output_schema=MatchExplanation,
        subject=("candidate", match.candidate_id),
        input_ref=f"match:{match.id}",
        bulk=True,
    )
    values: dict[str, Any] = {
        "explanation_action_id": result.action_id,
        "explanation_input_hash": match.input_hash,
    }
    if result.ok and isinstance(result.data, MatchExplanation):
        known = {e["id"] for e in evidence}
        if set(result.data.cited_evidence_ids) <= known:
            values.update(explanation=result.data.text, explanation_status="ready")
        else:
            values.update(explanation=None, explanation_status="failed")
    elif result.error_code in SKIP_CODES:
        values.update(explanation=None, explanation_status="skipped")
    else:
        values.update(explanation=None, explanation_status="failed")
    # Only the explanation columns are written (scores are never touched here).
    await db.execute(update(Match.__table__).where(Match.__table__.c.id == match.id).values(**values))  # type: ignore[arg-type]
    return str(values["explanation_status"])


async def explain_matches(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> dict[str, int]:
    settings = await effective(db)
    counts: dict[str, int] = {}
    if settings.explain_top_n <= 0:
        return counts
    profile = await demands.get_matching_profile(db, demand_id)
    if profile is None:
        return counts
    t = Match.__table__
    rows = list(
        await db.execute(
            select(
                t.c.id,
                t.c.candidate_id,
                t.c.sub_scores,
                t.c.evidence,
                t.c.input_hash,
                t.c.explanation_input_hash,
            )
            .where(t.c.demand_id == demand_id, t.c.dismissed_at.is_(None), t.c.is_stale.is_(False))
            .order_by(t.c.overall.desc(), t.c.candidate_id)
            .limit(settings.explain_top_n)
        )
    )
    summary = _demand_summary(profile)
    for row in rows:
        if row.explanation_input_hash == row.input_hash:
            continue
        status = await explain_one(ctx, db, row, summary)
        counts[status] = counts.get(status, 0) + 1
    return counts
