# ruff: noqa: E501, D401
"""Output checks and log redaction (Epic 53, AD-63, NFR-47).

* ``outbound(text, *, allowed_hosts, max_len)`` -- removes ``<untrusted_document>`` / internal tag names, strips links
  whose host is not allow-listed (``[link removed]``), caps the length. Agent drafts and messages pass it before saving.
* ``redact_for_log(obj)`` -- masks phone numbers, emails, Aadhaar, PAN, bank details and free text over 200 characters
  (``«redacted:n»``) for ``agent_tool_calls.args_redacted`` / ``result_summary`` and log lines.
* ``prescreen(ctx, db, text)`` -- fast-tier injection classifier (``injection_screen``); any failure is
  ``suspicious`` (fail-closed).
"""

import dataclasses
import re
from collections.abc import Iterable
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.settings import get_settings
from app.services.ai.redaction import redact

_INTERNAL_TAGS = (
    "untrusted_document",
    "system_notice",
    "system",
    "tool_result",
    "tool_use",
    "function_calls",
    "invoke",
)
_TAG_RE = re.compile(r"</?\s*(" + "|".join(_INTERNAL_TAGS) + r")\b[^>]*>", re.IGNORECASE)
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w])(?:\+?\d[\d\s().-]{8,}\d)(?![\w])")
LINK_REMOVED = "[link removed]"
FREE_TEXT_LIMIT = 200


@dataclasses.dataclass(frozen=True, slots=True)
class Sanitized:
    text: str
    stripped_links: int
    flags: tuple[str, ...]


def allowed_hosts(extra: Iterable[str] = ()) -> frozenset[str]:
    s = get_settings()
    hosts = {urlsplit(s.frontend_url).hostname or ""}
    if s.careers_root_domain:
        hosts.add(s.careers_root_domain.lower())
    hosts.update(h.lower() for h in extra if h)
    return frozenset(h for h in hosts if h)


def _host_ok(url: str, hosts: frozenset[str]) -> bool:
    target = url if "://" in url else f"https://{url}"
    host = (urlsplit(target).hostname or "").lower()
    return bool(host) and any(host == h or host.endswith(f".{h}") for h in hosts)


def outbound(text: str, *, allowed: frozenset[str] | None = None, max_len: int = 4000) -> Sanitized:
    hosts = allowed if allowed is not None else allowed_hosts()
    flags: list[str] = []
    cleaned, n_tags = _TAG_RE.subn("", text or "")
    if n_tags:
        flags.append("internal_tags_removed")
    stripped = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal stripped
        if _host_ok(m.group(0), hosts):
            return m.group(0)
        stripped += 1
        return LINK_REMOVED

    cleaned = _URL_RE.sub(sub, cleaned)
    if stripped:
        flags.append("links_stripped")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
        flags.append("truncated")
    return Sanitized(cleaned, stripped, tuple(flags))


def redact_text_for_log(value: str) -> str:
    if len(value) > FREE_TEXT_LIMIT:
        return f"«redacted:{len(value)}»"
    out, _ = redact(value)
    out = _EMAIL_RE.sub("«email»", out)
    return _PHONE_RE.sub("«phone»", out)


def redact_for_log(obj: Any) -> Any:
    """A copy of ``obj`` safe for log columns (NFR-47)."""
    if isinstance(obj, str):
        return redact_text_for_log(obj)
    if isinstance(obj, dict):
        return {str(k): redact_for_log(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact_for_log(v) for v in obj]
    return obj


def log_scan(text: str) -> list[str]:
    """Kinds of sensitive values found in ``text`` (53.6 log-scan test helper)."""
    found: list[str] = []
    _, counts = redact(text)
    found.extend(sorted(counts))
    if _EMAIL_RE.search(text):
        found.append("email")
    if _PHONE_RE.search(text):
        found.append("phone")
    return found


class InjectionVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["benign", "suspicious"]
    reasons: list[str] = Field(default_factory=list, max_length=10)


async def prescreen(ctx: RequestContext, db: AsyncSession, text: str) -> Literal["benign", "suspicious"]:
    """Fast-tier injection pre-screen of inbound conversation text / uploaded transcripts. Fail-closed."""
    from app.services import ai

    try:
        result = await ai.run(
            ctx,
            db,
            "agentic",
            "injection_screen",
            inputs={},
            untrusted={"text": text[:20_000]},
            output_schema=InjectionVerdict,
            bulk=True,
            model_tier="fast",
        )
    except Exception:
        return "suspicious"
    if not result.ok or not isinstance(result.data, InjectionVerdict):
        return "suspicious"
    return result.data.verdict


__all__ = [
    "LINK_REMOVED",
    "InjectionVerdict",
    "Sanitized",
    "allowed_hosts",
    "log_scan",
    "outbound",
    "prescreen",
    "redact_for_log",
    "redact_text_for_log",
]
