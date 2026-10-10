"""Shared plumbing of the portal service (Epic 9): the candidate principal, clocks, hashing, markdown,
stage labels, brand-colour contrast and the tenant / career-site resolution used by the public routes."""

import dataclasses
import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import nh3
from markdown_it import MarkdownIt

from app.core.context import RequestContext
from app.core.permissions import Principal

CANDIDATE_SIGN_IN = "candidate_sign_in"
DATA_EXPORT_DOWNLOAD = "data_export_download"


@dataclasses.dataclass(frozen=True, slots=True)
class CandidatePrincipal:
    """The signed-in candidate (``tos_candidate`` cookie)."""

    candidate_id: uuid.UUID
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    type: str = "candidate"

    @property
    def id(self) -> uuid.UUID:
        return self.candidate_id

    def core_principal(self) -> Principal:
        return Principal(
            type="candidate", id=self.candidate_id, session_id=self.session_id, tenant_id=self.tenant_id
        )

    def ctx(self, base: RequestContext | None = None) -> RequestContext:
        base = base or RequestContext()
        return dataclasses.replace(base, tenant_id=self.tenant_id, principal=self.core_principal())


def now() -> datetime:
    return datetime.now(UTC)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def ip_hash(ip: str | None) -> str:
    return sha256_hex(f"ip:{ip or 'unknown'}")[:32]


def email_hash(email: str, scope: str = "") -> str:
    return sha256_hex(f"{email.strip().lower()}|{scope.lower()}")[:32]


# ================================================================ markdown (about text, JD content)
_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "code", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "li", "ol", "p",
    "pre", "strong", "ul", "table", "thead", "tbody", "tr", "th", "td",
}  # fmt: skip
_ALLOWED_ATTRIBUTES = {"a": {"href", "title"}}


def render_markdown(text: str | None) -> str:
    """CommonMark (raw HTML disabled) -> allow-list sanitised HTML (nh3). Links get ``rel=noopener``."""
    if not text:
        return ""
    return nh3.clean(
        _md.render(text),
        tags=_ALLOWED_TAGS,
        attributes={k: set(v) for k, v in _ALLOWED_ATTRIBUTES.items()},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    )


_TAG = re.compile(r"<[^>]+>")


def plain_text(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", html)).strip()


# ================================================================ slugs
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(title: str, limit: int = 80) -> str:
    slug = _NON_SLUG.sub("-", (title or "").lower()).strip("-")
    return slug[:limit].strip("-") or "job"


# ================================================================ candidate stage labels
DEFAULT_STAGE_LABELS: dict[str, str] = {
    "applied": "Application received",
    "custom": "Under review",
    "shortlisted": "Under review",
    "submitted": "Under review",
    "interviewing": "Interviewing",
    "selected": "Interviewing",
    "offered": "Offer",
    "hired": "Hired",
    "rejected": "Not selected",
    "withdrawn": "Withdrawn",
}
TERMINAL_STAGES = frozenset({"hired", "rejected", "withdrawn"})


def stage_label(key: str, kind: str, tenant_labels: dict[str, Any] | None) -> str:
    """Candidate-facing label: tenant labels merged over the defaults; custom stages use the ``custom`` key.
    The internal stage key / label never leaves the service."""
    lookup = "custom" if kind == "custom" else key
    labels = {**DEFAULT_STAGE_LABELS, **{k: str(v) for k, v in (tenant_labels or {}).items() if v}}
    return labels.get(lookup) or DEFAULT_STAGE_LABELS["custom"]


# ================================================================ brand colour (WCAG contrast vs white)
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
MIN_CONTRAST = 4.5


def _channel(c: float) -> float:
    c = c / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)


def contrast_with_white(hex_color: str) -> float:
    r, g, b = _rgb(hex_color)
    lum = 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)
    return 1.05 / (lum + 0.05)


def darken_to_contrast(hex_color: str, target: float = MIN_CONTRAST) -> str:
    """Darken ``hex_color`` step by step (same hue) until it reaches ``target`` against white."""
    r, g, b = _rgb(hex_color)
    factor = 1.0
    candidate = hex_color
    while contrast_with_white(candidate) < target and factor > 0:
        factor -= 0.02
        candidate = "#{:02X}{:02X}{:02X}".format(*(max(0, int(c * factor)) for c in (r, g, b)))
    return candidate
