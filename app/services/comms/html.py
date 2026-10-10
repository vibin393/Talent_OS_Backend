"""HTML sanitising (nh3) and plain-text conversion for email bodies."""

import html
import re

import nh3

ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "a", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3", "h4", "hr", "i", "img", "li",
        "ol", "p", "pre", "s", "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
        "tr", "u", "ul",
    }
)  # fmt: skip
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan", "align"},
    "th": {"colspan", "rowspan", "align"},
    "table": {"width"},
}
URL_SCHEMES = {"http", "https", "mailto"}


def _attribute_filter(tag: str, attr: str, value: str) -> str | None:
    if tag == "img" and attr == "src" and not value.lower().startswith("https://"):
        return None  # images: https sources only
    return value


def sanitize_html(body: str) -> str:
    """Allow-list sanitiser: basic formatting, ``a[href]`` (http/https/mailto), tables, ``img`` (https)."""
    return nh3.clean(
        body or "",
        tags=set(ALLOWED_TAGS),
        attributes={k: set(v) for k, v in ALLOWED_ATTRIBUTES.items()},
        url_schemes=set(URL_SCHEMES),
        attribute_filter=_attribute_filter,
    )


_BLOCK_END = re.compile(r"</(p|div|h[1-6]|li|tr|blockquote|pre|table)\s*>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n{3,}")


def html_to_text(body: str) -> str:
    text = _BR.sub("\n", body or "")
    text = _BLOCK_END.sub("\n\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()
