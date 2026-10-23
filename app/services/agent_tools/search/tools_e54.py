# ruff: file-ignore[line-too-long]
"""Epic 54 Copilot read tool ``search.global`` (FR-277): the global quick search, groups gated by the user's own
permissions and scope (``search.global_search``)."""

from typing import Any

from pydantic import Field

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, as_uuid, plural, scoped

LINKABLE = frozenset({"candidate", "demand", "client", "placement", "worker", "project"})


class GlobalSearchIn(In):
    q: str = Field(min_length=2, max_length=120)
    limit: int = Field(default=5, ge=1, le=10)


class GlobalHit(Out):
    type: str
    id: str
    human_id: str | None
    title: str
    subtitle: str | None
    url: str | None


class GlobalSearchOut(Out):
    hits: list[GlobalHit]


async def _global(tctx: Any, args: GlobalSearchIn) -> ToolOutcome:
    from app.services.search import global_search

    out = await global_search.global_search(scoped(tctx), tctx.db, args.q, limit=args.limit)
    items = []
    if out.exact is not None:
        items.append(out.exact)
    for group in out.groups:
        items.extend(group.items)
    hits: list[GlobalHit] = []
    seen: set[tuple[str, str]] = set()
    refs = []
    cands = []
    for hit in items:
        key = (str(hit.type), str(hit.id))
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            GlobalHit(
                type=str(hit.type), id=str(hit.id), human_id=hit.human_id, title=hit.title[:200],
                subtitle=(hit.subtitle or None) and hit.subtitle[:200], url=hit.url if str(hit.type) in LINKABLE else None,
            )
        )  # fmt: skip
        refs.append((str(hit.type), hit.id))
        if str(hit.type) == "candidate" and (cid := as_uuid(hit.id)) is not None:
            cands.append(cid)
        if len(hits) >= args.limit * 3:
            break
    return ToolOutcome(
        GlobalSearchOut(hits=hits),
        refs=tuple(refs),
        candidate_ids=tuple(cands),
        summary=plural(len(hits), "result"),
    )


register_tool(
    ToolSpec(
        key="search.global",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Searched records",
        description=(
            "Quick search across candidates, demands, clients, placements and other records you can see, by name, "
            "REQ/human id, email or phone. Returns at most a few hits per record type with links. It does not rank, "
            "count everything or show records outside your access; use the list tools for filtered lists."
        ),
        input_model=GlobalSearchIn,
        output_model=GlobalSearchOut,
        permissions=(),
        risk="read",
        scope=ScopeRule(resource="none"),
        handler=_global,
        untrusted_output=frozenset({"hits.title", "hits.subtitle"}),
    )
)
