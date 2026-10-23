# ruff: file-ignore[line-too-long]
"""Write-up validation (Epic 61 A2, FR-305): plain text, ``sanitize.outbound`` (internal tags, foreign links), no
e-mail addresses / phone numbers, the Client rule's word cap and a deterministic protected-term scan
(``protected_terms.yaml``: protected attributes and compensation history). A hit raises ``ValidationFailed`` so the
agent rewrites (tool outcome ``invalid``) and a person sees the reason."""

import functools
import re
from pathlib import Path

import yaml

from app.core.errors import ValidationFailed

_TERMS_FILE = Path(__file__).with_name("protected_terms.yaml")
_TAG_RE = re.compile(r"<[^>]+>")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w])(?:\+?\d[\d\s().-]{8,}\d)(?![\w])")
_INVISIBLE_RE = re.compile(r"[​-‏⁠-⁤﻿󠀀-󠁿]")  # nosec B613 - intentional bidirectional-unicode detection regex (sanitizer)
MAX_CHARS = 3000


def _pattern(term: str) -> re.Pattern[str]:
    stem = term.rstrip("*").strip()
    tail = r"[a-z]*" if term.endswith("*") else r"(?![a-z])"
    return re.compile(rf"(?<![a-z]){re.escape(stem)}{tail}", re.IGNORECASE)


@functools.cache
def _terms() -> dict[str, tuple[tuple[str, re.Pattern[str]], ...]]:
    raw = yaml.safe_load(_TERMS_FILE.read_text(encoding="utf-8")) or {}
    return {group: tuple((t, _pattern(t)) for t in (items or [])) for group, items in raw.items()}


def protected_hits(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    lowered = f" {text or ''} "
    for group, terms in _terms().items():
        found = [t.rstrip("*") for t, rx in terms if rx.search(lowered)]
        if found:
            out[group] = found
    return out


def clean(text: str, *, max_words: int) -> tuple[str, tuple[str, ...]]:
    """Return the cleaned write-up and its flags, or raise ``ValidationFailed`` with a code the agent can act on."""
    from app.services.agent_runtime.sanitize import outbound

    plain = _INVISIBLE_RE.sub("", text or "")
    plain = _TAG_RE.sub("", plain).replace("\r\n", "\n").strip()
    result = outbound(plain, max_len=MAX_CHARS)
    flags = list(result.flags)
    cleaned = result.text
    if _EMAIL_RE.search(cleaned) or _PHONE_RE.search(cleaned):
        cleaned = _PHONE_RE.sub("[removed]", _EMAIL_RE.sub("[removed]", cleaned))
        flags.append("contact_details_removed")
    if not cleaned.strip():
        raise ValidationFailed("The write-up is empty", code="write_up_empty")
    hits = protected_hits(cleaned)
    if hits:
        raise ValidationFailed(
            "The write-up mentions a protected attribute or compensation history; remove it and save again.",
            code="write_up_protected_attribute",
            details={"terms": [t for group in hits.values() for t in group][:8], "groups": sorted(hits)},
        )
    words = len(cleaned.split())
    if words > max_words:
        raise ValidationFailed(
            f"The write-up has {words} words; the limit for this client is {max_words}.",
            code="write_up_too_long",
            details={"words": words, "max_words": max_words},
        )
    return cleaned, tuple(flags)


__all__ = ["MAX_CHARS", "clean", "protected_hits"]
