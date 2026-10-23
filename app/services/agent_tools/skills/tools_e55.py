# ruff: file-ignore[line-too-long]
"""Epic 55 ``skills.normalise`` (read, FR-282): map free-text skill labels to the tenant / platform catalogue through
aliases and slugs (``candidates.skills.resolve_skill(..., create_pending=False)`` -- **never creates** a skill) and,
with ``expand``, list related skills from the skills graph (``skills_service.explain_paths``)."""

import uuid
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services.agent_tools.demands.common_e55 import OWNER_EPIC, In, Out, scoped


class NormaliseIn(In):
    labels: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=40
    )
    expand: bool = False


class SkillMatch(Out):
    kind: Literal["global", "tenant"]
    id: uuid.UUID
    name: str
    status: str


class NormalisedItem(Out):
    label: str
    match: SkillMatch | None
    method: Literal["exact", "alias", "graph", "none"]
    related: list[str]


class NormaliseOut(Out):
    items: list[NormalisedItem]


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


async def _normalise(tctx: Any, args: NormaliseIn) -> ToolOutcome:
    from app.core.skill_refs import SkillKey
    from app.services import skills_service
    from app.services.candidates import skills as catalog

    ctx = scoped(tctx)
    items: list[NormalisedItem] = []
    for raw in args.labels:
        label = " ".join(str(raw).split())[:100]
        if not label:
            continue
        ref = await catalog.resolve_skill(ctx, tctx.db, label, create_pending=False)
        related: list[str] = []
        if ref is not None and args.expand:
            try:
                paths = await skills_service.explain_paths(
                    ctx, tctx.db, [SkillKey(kind=ref.kind, id=ref.id)], depth=1
                )
                related = [p.name for p in paths if p.key != f"{ref.kind}:{ref.id}"][:5]
            except Exception:  # the graph is optional
                related = []
        method: Any = "none" if ref is None else ("exact" if _norm(ref.name) == _norm(label) else "alias")
        items.append(
            NormalisedItem(
                label=label,
                match=SkillMatch(kind=ref.kind, id=ref.id, name=ref.name, status=ref.status)
                if ref is not None
                else None,  # type: ignore[arg-type]
                method=method,
                related=related,
            )
        )
    known = sum(1 for i in items if i.match is not None)
    return ToolOutcome(NormaliseOut(items=items), summary=f"{known} of {len(items)} skills matched")


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="skills.normalise", version=1, owner_epic=OWNER_EPIC, title="Normalise skills",
        description="Map up to 40 skill names to the skills catalogue (exact names and aliases); with expand=true also list up to 5 related skills from the skills graph. Unknown names come back unmatched -- suggest them as new skills. It never creates or changes a skill.",
        input_model=NormaliseIn, output_model=NormaliseOut, permissions=("demand:read",), risk="read",
        scope=ScopeRule(resource="none"), handler=_normalise,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
