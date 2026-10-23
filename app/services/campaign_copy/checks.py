# ruff: file-ignore[line-too-long]
"""Deterministic copy checks (Epic 78 A4, FR-403). PURE: no database, no LLM; re-run on every edit.

``run(draft, context) -> list[Finding]`` where ``draft`` carries ``channel, subject, preheader, body_html,
template_values, job_link_blocks`` and ``context`` (``CheckContext``) the linked postings' public fields, the brief,
the allowed link hosts and (SMS / WhatsApp) the chosen template. ``block`` findings refuse approval; ``warn``
findings need an explicit acknowledgement.
"""

import dataclasses
import html
import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

BLOCK = "block"
WARN = "warn"
MAX_SUBJECT = 150
MAX_PREHEADER = 200
MAX_BODY_BYTES = 51_200
SPAM_SUBJECT = 78
DLT_VALUE_MAX = 30
WA_VALUE_MAX = 1024
# The P3 campaign renderer resolves recipient, Company and sender fields (``entity_refs.merge_data`` with a
# candidate addressee) plus ``{{job_link.N}}`` (``sending.replace_job_link_tokens``). Recorded in the spec notes (A0).
ALLOWED_MERGE_GROUPS = frozenset({"candidate", "tenant", "sender"})

# ---------------------------------------------------------------- lexicons
_PROTECTED: tuple[str, ...] = (
    # gender
    r"(?:only|just)\s+(?:male|female|men|women|boys|girls)", r"(?:male|female|men|women|boys|girls)\s+(?:only|candidates only|preferred)",
    r"gender", r"\bpreferably\s+(?:male|female)",
    # age
    r"(?:age|aged)\s*(?:below|under|above|over|between|limit|:)?\s*\d{2}", r"(?:below|under|above|over)\s+\d{2}\s*(?:years|yrs)\s*(?:of age|old)",
    r"\d{2}\s*(?:years|yrs)\s+old", r"\bage\s+limit\b",
    # religion / caste
    r"religio\w*", r"\b(?:hindu|muslim|christian|sikh|jain|buddhist|parsi)s?\b", r"\bcaste\b", r"\bbrahmin\w*", r"\bdalit\w*",
    # marital status / pregnancy / family
    r"marital\s+status", r"\b(?:un)?married\b", r"\bsingle\s+(?:only|candidates)", r"pregnan\w*", r"\bno\s+children\b",
    # disability
    r"disabilit\w*", r"handicap\w*", r"able[- ]bodied", r"physically\s+fit",
    # nationality preferences
    r"nationality", r"(?:indians|locals|natives|citizens)\s+only", r"only\s+(?:indians|locals|natives|citizens)",
    # transliterated Hindi
    r"sirf\s+(?:ladke|ladkiyan|ladkiyon|mard|aurat)", r"(?:ladke|ladkiyan|ladkiyon)\s+(?:hi|chahiye)", r"shaadi[- ]?shuda", r"avivahit",
    r"\bjaati\b", r"\bdharm\b", r"\bumar\s+\d{2}",
)
_PROTECTED_RE = [re.compile(rf"(?i)(?<![\w]){p}") for p in _PROTECTED]

_BIASED: tuple[str, ...] = (
    "rockstar", "rock star", "ninja", "guru", "young and dynamic", "energetic youngster", "digital native", "native speaker",
    "manpower", "salesman", "chairman", "aggressive", "dominant", "recent graduates only", "fresh blood",
)
_BIASED_RE = [re.compile(rf"(?i)\b{re.escape(t)}\b") for t in _BIASED]

_CLAIMS: dict[str, tuple[str, ...]] = {
    "remote": ("remote", "work from home", "wfh", "work-from-home", "hybrid"),
    "visa": ("visa sponsorship", "visa sponsored", "sponsor your visa", "h1b", "work permit"),
    "joining_bonus": ("joining bonus", "sign-on bonus", "signing bonus", "sign on bonus"),
    "relocation": ("relocation", "relocate", "relocation assistance"),
    "benefits": ("health insurance", "medical insurance", "free meals", "stock options", "esops", "esop", "unlimited leave", "paid leave"),
}
_CITIES = (
    "bengaluru", "bangalore", "mumbai", "pune", "hyderabad", "chennai", "delhi", "new delhi", "gurgaon", "gurugram", "noida",
    "kolkata", "ahmedabad", "jaipur", "kochi", "chandigarh", "indore", "coimbatore", "lucknow", "nagpur", "mysuru", "mysore",
)
_SPAM_WORDS = ("guaranteed", "100% free", "act now", "risk-free", "risk free", "click here", "winner", "urgent!!!", "limited time offer", "earn money fast")

_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"')\]]+")
_HREF_RE = re.compile(r"(?i)(?:href|src)\s*=\s*[\"']?([^\"'\s>]+)")
_TOKEN_RE = re.compile(r"\{\{(.*?)\}\}", re.S)
_FIELD_RE = re.compile(r"^\s*([a-z_]+)\.([a-z_]+)\s*$")
_JOB_LINK_RE = re.compile(r"^\s*job_link\.(\d+)\s*$")
_UNSAFE_RE = (
    re.compile(r"(?i)<\s*script\b"), re.compile(r"(?i)<\s*form\b"), re.compile(r"(?i)<\s*iframe\b"), re.compile(r"(?i)<\s*object\b"),
    re.compile(r"(?i)<\s*embed\b"), re.compile(r"(?i)\bon[a-z]+\s*="), re.compile(r"(?i)javascript\s*:"), re.compile(r"(?i)<\s*link\b"),
    re.compile(r"(?i)@import"), re.compile(r"(?i)<\s*meta\b"), re.compile(r"(?i)<\s*base\b"),
)
_HIDDEN_RE = re.compile(r"(?i)display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0")
_UNSUB_RE = re.compile(r"(?i)unsubscribe|opt[- ]?out|\bstop\s+(?:receiving|emails)")
_OPTOUT_SMS_RE = re.compile(r"(?i)\bstop\b|opt[- ]?out|unsubscribe|\bto\s+stop\b")

_NUM = r"(\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)"
_MONEY_RES = (
    # ₹ / Rs / INR amounts with optional scale
    re.compile(rf"(?i)(?:₹|\brs\.?|\binr)\s*{_NUM}\s*(lpa|lakhs?|lacs?|lac|l|k|cr|crores?)?\b"),
    # amounts with an Indian pay unit (LPA, lakh, CTC, crore)
    re.compile(rf"(?i)\b{_NUM}\s*(lpa|lakhs?|lacs?|lac|cr|crores?)\b"),
    re.compile(rf"(?i)\bctc\s*(?:of|upto|up to|:)?\s*{_NUM}\s*(lpa|lakhs?|lacs?|lac|k|cr)?"),
    # per-hour / per-month figures
    re.compile(rf"(?i)\b{_NUM}\s*(k)?\s*(?:/|per\s+|an\s+|a\s+)(?:hr|hour|month|mo|annum|year)\b"),
    # Hinglish: "salary 18 tak", "18 hazaar"
    re.compile(rf"(?i)\b{_NUM}\s*(hazaar|hazar|hajar)\b"),
)
_SCALE = {
    "lpa": Decimal(100_000), "lakh": Decimal(100_000), "lakhs": Decimal(100_000), "lac": Decimal(100_000), "lacs": Decimal(100_000),
    "l": Decimal(100_000), "k": Decimal(1000), "cr": Decimal(10_000_000), "crore": Decimal(10_000_000), "crores": Decimal(10_000_000),
    "hazaar": Decimal(1000), "hazar": Decimal(1000), "hajar": Decimal(1000),
}


@dataclasses.dataclass(frozen=True, slots=True)
class PostingFacts:
    posting_id: str
    title: str = ""
    location: str | None = None
    work_mode: str | None = None
    show_rate: bool = False
    pay_min: Decimal | None = None
    pay_max: Decimal | None = None
    description: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class TemplateFacts:
    body: str
    variables: tuple[str, ...]
    url_variables: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class CheckContext:
    channel: str
    postings: tuple[PostingFacts, ...] = ()
    brief_text: str = ""
    allowed_hosts: frozenset[str] = frozenset()
    template: TemplateFacts | None = None


def finding(code: str, severity: str, field: str, excerpt: str = "") -> dict[str, str]:
    return {"code": code, "severity": severity, "field": field, "excerpt": excerpt[:160]}


def _plain(value: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", value or "")).split())


def _fields(draft: Mapping[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name in ("subject", "preheader"):
        if draft.get(name):
            out.append((name, str(draft[name])))
    if draft.get("body_html"):
        out.append(("body_html", _plain(str(draft["body_html"]))))
    for key, value in (draft.get("template_values") or {}).items():
        out.append((f"template_values.{key}", str(value)))
    for i, block in enumerate(draft.get("job_link_blocks") or []):
        if isinstance(block, Mapping) and block.get("cta"):
            out.append((f"job_link_blocks[{i}].cta", str(block["cta"])))
    return out


def _host_ok(url: str, hosts: frozenset[str]) -> bool:
    if url.startswith(("#", "mailto:", "tel:")) or url.startswith("{{"):
        return True
    target = url if "://" in url else f"https://{url}"
    host = (urlsplit(target).hostname or "").lower()
    return bool(host) and any(host == h or host.endswith(f".{h}") for h in hosts)


def _amount(raw: str, unit: str | None) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    if unit:
        value *= _SCALE.get(unit.lower(), Decimal(1))
    return value


_RANGE_RE = re.compile(
    rf"(?i)(?:₹|\brs\.?|\binr)?\s*{_NUM}\s*(?:-|–|—|to)\s*(?:₹|\brs\.?|\binr)?\s*{_NUM}\s*(lpa|lakhs?|lacs?|lac|k|cr|crores?)\b"
)


def pay_figures(text: str) -> list[tuple[Decimal, str]]:
    """Every pay figure in ``text`` as a number (scale applied; a range's unit applies to both ends)."""
    found: list[tuple[Decimal, str]] = []
    seen: set[tuple[int, int]] = set()
    for m in _RANGE_RE.finditer(text):
        seen.add((m.start(), m.end()))
        for raw in (m.group(1), m.group(2)):
            value = _amount(raw, m.group(3))
            if value is not None:
                found.append((value, m.group(0).strip()))
    for rx in _MONEY_RES:
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in seen):
                continue
            seen.add((m.start(), m.end()))
            unit = next((g for g in m.groups()[1:] if g), None)
            value = _amount(m.group(1), unit)
            if value is not None:
                found.append((value, m.group(0)))
    return found


def _in_range(value: Decimal, postings: Iterable[PostingFacts]) -> bool:
    for p in postings:
        if not p.show_rate or (p.pay_min is None and p.pay_max is None):
            continue
        low = p.pay_min if p.pay_min is not None else p.pay_max
        high = p.pay_max if p.pay_max is not None else p.pay_min
        assert low is not None and high is not None
        if low <= value <= high:
            return True
    return False


def _claim_supported(terms: tuple[str, ...], context: CheckContext, key: str) -> bool:
    corpus = " ".join(
        [context.brief_text, *(f"{p.title} {p.location or ''} {p.work_mode or ''} {p.description}" for p in context.postings)]
    ).lower()
    if key == "remote" and any((p.work_mode or "").lower() in ("remote", "hybrid") for p in context.postings):
        return True
    return any(t in corpus for t in terms)


def _merge_findings(raw: str, field: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for m in _TOKEN_RE.finditer(raw or ""):
        inner = m.group(1)
        if _JOB_LINK_RE.match(inner):
            continue
        fm = _FIELD_RE.match(inner)
        if fm is None or fm.group(1) not in ALLOWED_MERGE_GROUPS or not _known_field(f"{fm.group(1)}.{fm.group(2)}"):
            out.append(finding("unknown_merge_field", BLOCK, field, m.group(0)))
    rest = _TOKEN_RE.sub("", raw or "")
    if "{{" in rest or "}}" in rest:
        out.append(finding("unknown_merge_field", BLOCK, field, "{{"))
    return out


def _known_field(path: str) -> bool:
    from app.services.comms.merge_fields import MERGE_FIELDS

    return path in MERGE_FIELDS


def run(draft: Mapping[str, Any], context: CheckContext) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    channel = context.channel
    texts = _fields(draft)
    raw_fields: list[tuple[str, str]] = [
        (n, str(draft.get(n) or "")) for n in ("subject", "preheader", "body_html") if draft.get(n)
    ] + [(f"template_values.{k}", str(v)) for k, v in (draft.get("template_values") or {}).items()]

    # protected attributes (block) / biased terms (warn)
    for field, text in texts:
        for rx in _PROTECTED_RE:
            m = rx.search(text)
            if m:
                out.append(finding("protected_attribute", BLOCK, field, m.group(0)))
                break
        for rx in _BIASED_RE:
            m = rx.search(text)
            if m:
                out.append(finding("biased_term", WARN, field, m.group(0)))
                break

    # compensation (block)
    for field, text in texts:
        for value, excerpt in pay_figures(text):
            if not _in_range(value, context.postings):
                out.append(finding("compensation_out_of_range", BLOCK, field, excerpt))
                break

    # unsupported claims (warn)
    for key, terms in _CLAIMS.items():
        for field, text in texts:
            low = text.lower()
            hit = next((t for t in terms if re.search(rf"(?<![\w]){re.escape(t)}(?![\w])", low)), None)
            if hit and not _claim_supported(terms, context, key):
                out.append(finding("unsupported_claim", WARN, field, hit))
                break
    for field, text in texts:
        low = text.lower()
        for city in _CITIES:
            if re.search(rf"\b{re.escape(city)}\b", low) and not _claim_supported((city,), context, "city"):
                out.append(finding("unsupported_claim", WARN, field, city))
                break

    # links (block)
    for field, raw in raw_fields:
        urls = [m.group(1) for m in _HREF_RE.finditer(raw)] + [m.group(0) for m in _URL_RE.finditer(_TAG_RE.sub(" ", raw))]
        bad = next((u for u in urls if not _host_ok(u, context.allowed_hosts)), None)
        if bad:
            out.append(finding("foreign_link", BLOCK, field, bad))

    # merge fields (block)
    for field, raw in raw_fields:
        out.extend(_merge_findings(raw, field)[:1])

    body = str(draft.get("body_html") or "")
    if channel == "email":
        if _UNSUB_RE.search(body) or _HIDDEN_RE.search(body):
            m = _UNSUB_RE.search(body) or _HIDDEN_RE.search(body)
            out.append(finding("unsubscribe_tampering", BLOCK, "body_html", m.group(0) if m else ""))
        unsafe = next((rx.search(body) for rx in _UNSAFE_RE if rx.search(body)), None)
        if unsafe:
            out.append(finding("html_unsafe", BLOCK, "body_html", unsafe.group(0)))
        if not (draft.get("subject") or "").strip():
            out.append(finding("length", BLOCK, "subject", "missing"))
        if len(draft.get("subject") or "") > MAX_SUBJECT:
            out.append(finding("length", BLOCK, "subject", f"{len(draft.get('subject') or '')} characters"))
        if len(draft.get("preheader") or "") > MAX_PREHEADER:
            out.append(finding("length", BLOCK, "preheader", f"{len(draft.get('preheader') or '')} characters"))
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            out.append(finding("length", BLOCK, "body_html", "over 50 KB"))
        if not body.strip():
            out.append(finding("length", BLOCK, "body_html", "missing"))

    # templates (SMS / WhatsApp)
    if channel in ("sms", "whatsapp"):
        tpl = context.template
        values = {str(k): str(v) for k, v in (draft.get("template_values") or {}).items()}
        if tpl is None:
            out.append(finding("template_variable_limit", BLOCK, "template_values", "no template"))
        else:
            if channel == "sms" and not _OPTOUT_SMS_RE.search(tpl.body or ""):
                out.append(finding("missing_optout_sms", BLOCK, "template", "no opt-out text"))
            missing = [v for v in tpl.variables if not values.get(v, "").strip()]
            extra = [k for k in values if k not in tpl.variables]
            if missing or extra:
                out.append(finding("template_variable_limit", BLOCK, "template_values", ", ".join([*missing, *extra])))
            for key, value in values.items():  # type: ignore[assignment]
                field = f"template_values.{key}"
                if channel == "sms":
                    if len(value) > DLT_VALUE_MAX:  # type: ignore[arg-type]
                        out.append(finding("template_variable_limit", BLOCK, field, f"{len(value)} characters"))  # type: ignore[arg-type]
                    elif _URL_RE.search(value) and key not in tpl.url_variables:  # type: ignore[call-overload]
                        out.append(finding("template_variable_limit", BLOCK, field, "URL in a text variable"))
                else:
                    if len(value) > WA_VALUE_MAX:  # type: ignore[arg-type]
                        out.append(finding("template_variable_limit", BLOCK, field, f"{len(value)} characters"))  # type: ignore[arg-type]
                    elif "\n" in value or "\t" in value or "    " in value:  # type: ignore[operator]
                        out.append(finding("template_variable_limit", BLOCK, field, "newline, tab or 4+ spaces"))

    # spam heuristics (warn)
    subject = str(draft.get("subject") or "")
    if len(subject) > SPAM_SUBJECT:
        out.append(finding("spam_heuristics", WARN, "subject", f"{len(subject)} characters"))
    for field, text in texts:
        letters = [c for c in text if c.isalpha()]
        if len(letters) >= 10 and sum(c.isupper() for c in letters) / len(letters) > 0.3:
            out.append(finding("spam_heuristics", WARN, field, "mostly capital letters"))
            break
    all_text = " ".join(t for _, t in texts)
    if all_text.count("!") >= 3:
        out.append(finding("spam_heuristics", WARN, "body_html", "3 or more '!'"))
    spam = next((w for w in _SPAM_WORDS if w in all_text.lower()), None)
    if spam:
        out.append(finding("spam_heuristics", WARN, "body_html", spam))
    return out


def blocked(findings: Iterable[Mapping[str, Any]]) -> bool:
    return any(f.get("severity") == BLOCK for f in findings)


def has_warnings(findings: Iterable[Mapping[str, Any]]) -> bool:
    return any(f.get("severity") == WARN for f in findings)


__all__ = [
    "ALLOWED_MERGE_GROUPS",
    "BLOCK",
    "WARN",
    "CheckContext",
    "PostingFacts",
    "TemplateFacts",
    "blocked",
    "finding",
    "has_warnings",
    "pay_figures",
    "run",
]
