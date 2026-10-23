# ruff: file-ignore[line-too-long]
"""Copilot <-> agent runtime bridge (Epic 54 §0.2). With coordinator decision C-2 the foundation exposes
``RunOut.related_refs`` / ``RunOut.candidate_ids`` and ``agent_runtime_service.purge_run_content``, so this module only
calls the runtime facade -- no direct runtime table access for refs or content purge."""

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.services import agent_runtime_service as runtime

# Record types an answer may link to (citations are validated against these refs in the UI).
LINKABLE_REF_TYPES = frozenset(
    {"candidate", "demand", "application", "client", "placement", "interview", "worker", "project", "contact"}
)
MAX_REFS = 200


def refs_of(run: Any) -> list[list[str]]:
    """``[[type, id], ...]`` of a ``RunOut`` restricted to linkable types (<= 200)."""
    out: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for ref in getattr(run, "related_refs", None) or []:
        if not isinstance(ref, list | tuple) or len(ref) != 2:
            continue
        kind, rid = str(ref[0]), str(ref[1])
        if kind in LINKABLE_REF_TYPES and (kind, rid) not in seen:
            seen.add((kind, rid))
            out.append([kind, rid])
        if len(out) >= MAX_REFS:
            break
    return out


def candidate_ids_of(run: Any) -> list[uuid.UUID]:
    return [uuid.UUID(str(c)) for c in getattr(run, "candidate_ids", None) or []]


async def purge(ctx: Any, db: AsyncSession, run_ids: Iterable[uuid.UUID]) -> int:
    """Erase the stored prompts / outputs of the given Copilot runs (payloads, redacted args, summaries, result);
    run metadata and counters stay (C-2 ``purge_run_content``)."""
    removed = 0
    for run_id in dict.fromkeys(run_ids):
        try:
            removed += await runtime.purge_run_content(ctx, db, run_id)
        except DomainError:  # already gone / not visible: nothing to purge
            continue
    return removed


async def discard_drafts(ctx: Any, db: AsyncSession, proposal_ids: Sequence[uuid.UUID]) -> int:
    """Discard the still-pending Copilot drafts among ``proposal_ids`` (as their owner)."""
    discarded = 0
    for pid in proposal_ids:
        try:
            p = await runtime.get_proposal(ctx, db, pid)
            if p.status != "pending":
                continue
            async with db.begin_nested():
                await runtime.discard_draft(ctx, db, pid)
            discarded += 1
        except DomainError:
            continue
    return discarded


__all__ = ["LINKABLE_REF_TYPES", "candidate_ids_of", "discard_drafts", "purge", "refs_of"]
