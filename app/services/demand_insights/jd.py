# ruff: noqa: E501
"""JD preview and save wrappers (Epic 55, story 55.3, FR-282).

* ``preview`` -- the P1 generation inputs (``demands.jd._generation_inputs``) + **``demands_jd_generate.v2``** (adds
  the tenant template and the inclusive-language line), feature ``agent_demand``, standard tier; then the P1
  ``bias_matches``. **Saves nothing.** The P1 JD generation stays pinned to ``demands_jd_generate.v1``.
* ``save_ai_version`` -- a new JD version authored by ``ai`` (never published: publishing is a person's action).
"""

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.models.demands import Demand, DemandSkill, JdVersion

PROMPT = "demands_jd_generate.v2"
FEATURE = "agent_demand"
SUGGESTIONS = {
    "age": "Use age-neutral wording (describe the skills and experience needed)",
    "gender": "Use gender-neutral wording",
    "marital": "Remove marital or family status",
    "religion": "Remove religious preferences",
    "caste": "Remove caste references",
    "nationality": "Describe the work authorisation needed instead",
    "disability": "Describe the essential tasks instead",
    "appearance": "Remove appearance preferences",
    "other": "Use neutral, inclusive wording",
}


async def _skills(db: AsyncSession, demand_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(DemandSkill).where(DemandSkill.demand_id == demand_id).order_by(DemandSkill.position)
        )
    ).scalars()
    return [{"label": s.skill_label, "kind": s.kind, "min_years": s.min_years} for s in rows]


async def has_jd(db: AsyncSession, demand_id: uuid.UUID) -> JdVersion | None:
    return (
        await db.execute(
            select(JdVersion)
            .where(JdVersion.demand_id == demand_id)
            .order_by(JdVersion.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def bias_matches(db: AsyncSession, content: str) -> list[dict[str, str]]:
    from app.services.demands.jd import bias_matches as p1_bias

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for m in await p1_bias(db, content):
        key = m.term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"term": m.term, "suggestion": SUGGESTIONS.get(m.category, SUGGESTIONS["other"])})
    return out[:30]


async def preview(
    ctx: RequestContext, db: AsyncSession, demand: Demand, kind: str, template: str | None
) -> tuple[str, list[dict[str, str]], uuid.UUID | None]:
    from app.schemas.demands import JdDraft, JdGenerateIn
    from app.services import ai
    from app.services.ai import fake_llm
    from app.services.ai.gateway import escape_untrusted
    from app.services.demands.jd import _generation_inputs

    fields = _generation_inputs(
        demand, await _skills(db, demand.id), JdGenerateIn(kind=kind, show_rate=False)
    )  # type: ignore[arg-type]
    token = fake_llm.CURRENT_PROMPT.set(
        "demands_jd_generate"
    )  # the scripted Fake LLM serves this call (tests)
    try:
        result = await ai.run(
            ctx, db, FEATURE, PROMPT, inputs={"kind": kind, "template": escape_untrusted(template or "")},
            untrusted={"demand_fields": json.dumps(fields, default=str, ensure_ascii=False, indent=1)},
            output_schema=JdDraft, subject=("demand", demand.id), input_ref=f"demand:{demand.id}",
        )  # fmt: skip
    finally:
        fake_llm.CURRENT_PROMPT.reset(token)
    if not result.ok or not isinstance(result.data, JdDraft):
        raise ConflictError(
            "AI is unavailable right now; continue manually",
            code="ai_unavailable",
            details={"reason": result.error_code},
        )
    content = result.data.content
    return content, await bias_matches(db, content), result.action_id


async def save_ai_version(
    ctx: RequestContext, db: AsyncSession, demand: Demand, content: str, *, kind: str = "full"
) -> uuid.UUID:
    from app.services.demands.common import timeline
    from app.services.demands.jd import _add_version

    row = await _add_version(ctx, db, demand, kind=kind, content=content, author_type="ai")
    await timeline(
        ctx,
        db,
        demand.id,
        f"JD drafted by the Demand Agent (version {row.version_no}, {kind})",
        actor_kind="agent",
    )
    return row.id


__all__ = ["FEATURE", "PROMPT", "bias_matches", "has_jd", "preview", "save_ai_version"]
