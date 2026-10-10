# ruff: noqa: E501
"""Copilot turn kickoff (Epic 54 §0.1, AD-64). One run per user question; the conversation is the context.

* ``Kickoff.inputs`` (trusted -- the user is both owner and instruction source): ``question`` (<= 4,000 chars),
  ``page_context`` (ids and labels JSON), ``today`` (tenant-local date), ``fy_start_month`` = "4",
  ``user_display_name`` and, when capabilities are on, ``capability_instructions`` (their static prompt sections).
* ``Kickoff.untrusted``: ``prior_turns`` -- earlier questions and answers as text, oldest first, capped at 60,000
  characters (NFR-48 drives the segment size; the static system prompt is cached). When older turns are cut, the
  conversation's ``context_summary`` is placed first. Prior answers may echo tool text, so a follow-up run is tainted;
  that changes nothing for the Copilot because every write is a draft.
"""

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.core.agentic import Kickoff

PRIOR_TURNS_MAX_CHARS = 60_000
QUESTION_MAX_CHARS = 4000


def page_context_json(page_context: Mapping[str, Any] | None) -> str:
    if not page_context:
        return "{}"
    return json.dumps(dict(page_context), sort_keys=True, default=str, separators=(",", ":"))[:6000]


def _turn_text(role: str, content: Mapping[str, Any]) -> str | None:
    if content.get("redacted"):
        return None
    text = str(content.get("text") or "").strip()
    if not text:
        return None
    return f"{'User' if role == 'user' else 'Copilot'}: {text}"


def prior_turns(
    messages: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    summary: str | None,
    max_chars: int = PRIOR_TURNS_MAX_CHARS,
) -> tuple[str, bool]:
    """Text of earlier turns (user + assistant only), newest kept when over ``max_chars``; returns (text, cut)."""
    lines = [
        t for role, content in messages if role in ("user", "assistant") and (t := _turn_text(role, content))
    ]
    kept: list[str] = []
    used = 0
    cut = False
    for line in reversed(lines):
        if used + len(line) + 1 > max_chars:
            cut = True
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    if cut and summary:
        head = f"Summary of earlier turns: {summary}"[: max(0, max_chars - used - 1)]
        kept.insert(0, head)
    return "\n".join(kept), cut


def build_kickoff(
    *,
    question: str,
    page_context: Mapping[str, Any] | None,
    today: str,
    user_display_name: str,
    prior: Sequence[tuple[str, Mapping[str, Any]]] = (),
    summary: str | None = None,
    candidate_ids: Iterable[uuid.UUID] = (),
    capability_instructions: str | None = None,
) -> tuple[Kickoff, bool]:
    inputs = {
        "question": question[:QUESTION_MAX_CHARS],
        "page_context": page_context_json(page_context),
        "today": today,
        "fy_start_month": "4",
        "user_display_name": user_display_name[:120],
    }
    if capability_instructions:
        inputs["capability_instructions"] = capability_instructions
    text, cut = prior_turns(prior, summary=summary)
    untrusted = {"prior_turns": text} if text else {}
    return Kickoff(inputs=inputs, untrusted=untrusted, candidate_ids=tuple(dict.fromkeys(candidate_ids))), cut


def page_candidate_ids(page_context: Mapping[str, Any] | None) -> list[uuid.UUID]:
    """Candidate ids named by the page context (feeds the run's ``candidate_ids`` for erasure)."""
    if not page_context:
        return []
    out: list[uuid.UUID] = []
    if page_context.get("entity_type") == "candidate" and page_context.get("entity_id"):
        out.append(uuid.UUID(str(page_context["entity_id"])))
    lst = page_context.get("list") or {}
    if lst.get("kind") == "candidates":
        out.extend(uuid.UUID(str(i)) for i in lst.get("selected_ids") or [])
    return out


__all__ = ["PRIOR_TURNS_MAX_CHARS", "build_kickoff", "page_candidate_ids", "page_context_json", "prior_turns"]
