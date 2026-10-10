"""Email merge fields (FR-54): ``{{ group.field }}`` tokens only -- no logic, loops or filters.

* ``MERGE_FIELDS`` is the closed catalog (label, sample value, resolver). Compensation is never a field.
* ``ALLOWED_GROUPS_BY_KEY`` limits the groups a template key may use (``interview.*`` only in ``interview_*``
  and ``custom`` templates, ...).
* ``validate_tokens`` refuses unknown / disallowed tokens (422 ``unknown_merge_field``, ``details.fields``)
  and malformed braces (422 ``malformed_merge_token``).
* ``render`` substitutes values with a regex; values are HTML-escaped in bodies. Missing values render empty
  and are reported in the returned warnings.
"""

import dataclasses
import html
import re
from collections.abc import Callable, Iterable, Mapping

from app.core.errors import ValidationFailed

MergeData = Mapping[str, Mapping[str, str | None]]

TOKEN_RE = re.compile(r"\{\{(.*?)\}\}", re.S)
FIELD_RE = re.compile(r"^\s*([a-z_]+)\.([a-z_]+)\s*$")


@dataclasses.dataclass(frozen=True, slots=True)
class MergeField:
    label: str
    sample: str
    resolver: Callable[[MergeData], str | None]


def _resolver(group: str, field: str) -> Callable[[MergeData], str | None]:
    def resolve(data: MergeData) -> str | None:
        value = (data.get(group) or {}).get(field)
        return None if value is None else str(value)

    return resolve


_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("candidate.first_name", "Candidate first name", "Priya"),
    ("candidate.full_name", "Candidate full name", "Priya Sharma"),
    ("candidate.email", "Candidate email", "priya.sharma@example.com"),
    ("contact.first_name", "Contact first name", "Rahul"),
    ("contact.full_name", "Contact full name", "Rahul Mehta"),
    ("client.name", "Client name", "Acme Corp"),
    ("demand.title", "Demand title", "Senior Data Engineer"),
    ("demand.human_id", "Demand ID", "REQ-1056"),
    ("demand.location", "Demand location", "Bengaluru"),
    ("sender.name", "Your name", "Anita Rao"),
    ("sender.email", "Your email", "anita.rao@example.com"),
    ("sender.title", "Your title", "Senior Recruiter"),
    ("sender.signature", "Your signature", "Anita Rao | Talent Team"),
    ("tenant.name", "Company name", "Initech Talent"),
    ("interview.start_local", "Interview start (local time)", "Tue, 14 Oct 2026, 11:00"),
    ("interview.timezone", "Interview timezone", "Asia/Kolkata"),
    ("interview.mode", "Interview mode", "Video call"),
    ("interview.join_link", "Interview join link", "https://meet.example.com/abc-defg-hij"),
    ("offer.link", "Offer link", "https://app.example.com/l/offer-token"),
    ("link.url", "Action link", "https://app.example.com/l/secure-token"),
    # Epic 57: the Screening Agent chat (Candidate Portal or Secure Link) in the AI screening invitation.
    ("screening.chat_link", "AI screening chat link", "https://app.example.com/l/chat-token"),
    # Epic 59: the Scheduling Agent's self-scheduling ("pick a time") link.
    ("scheduling.pick_link", "Self-scheduling link", "https://app.example.com/l/pick-token"),
    ("task.title", "Task title", "Call the hiring manager"),
    ("task.due_local", "Task due (local time)", "Wed, 15 Oct 2026, 17:00"),
)

MERGE_FIELDS: dict[str, MergeField] = {
    path: MergeField(label, sample, _resolver(*path.split(".", 1))) for path, label, sample in _CATALOG
}

GROUP_LABELS: dict[str, str] = {
    "candidate": "Candidate",
    "contact": "Contact",
    "client": "Client",
    "demand": "Demand",
    "sender": "Sender",
    "tenant": "Company",
    "interview": "Interview",
    "offer": "Offer",
    "link": "Link",
    "task": "Task",
    "screening": "AI screening",
    "scheduling": "Self-scheduling",
}

_COMMON = ("sender", "tenant", "link")
ALLOWED_GROUPS_BY_KEY: dict[str, frozenset[str]] = {
    "submission": frozenset({*_COMMON, "candidate", "contact", "client", "demand"}),
    "interview_invite": frozenset({*_COMMON, "candidate", "contact", "client", "demand", "interview", "scheduling"}),
    "interview_reminder": frozenset({*_COMMON, "candidate", "contact", "client", "demand", "interview", "scheduling"}),
    "rejection": frozenset({"sender", "tenant", "candidate", "client", "demand"}),
    "offer": frozenset({*_COMMON, "candidate", "client", "demand", "offer"}),
    "application_confirmation": frozenset({*_COMMON, "candidate", "client", "demand"}),
    "ai_screening_invite": frozenset({*_COMMON, "candidate", "demand", "screening"}),
    "client_feedback_request": frozenset({*_COMMON, "candidate", "contact", "client", "demand"}),
    "custom": frozenset(GROUP_LABELS),
    "voice_consent_request": frozenset({*_COMMON, "candidate", "demand"}),  # Epic 58
}

# Groups whose values depend on the recipient: using them splits a send into one message per recipient.
RECIPIENT_GROUPS = frozenset({"candidate", "contact"})


def _malformed() -> ValidationFailed:
    return ValidationFailed("A merge field is malformed; use {{ group.field }}", code="malformed_merge_token")


def tokens(text: str) -> list[str]:
    """Merge-field paths used in ``text`` (in order, de-duplicated); 422 when braces are malformed."""
    found: list[str] = []
    for match in TOKEN_RE.finditer(text or ""):
        field = FIELD_RE.match(match.group(1))
        if field is None:
            raise _malformed()
        found.append(f"{field.group(1)}.{field.group(2)}")
    remainder = TOKEN_RE.sub("", text or "")
    if "{{" in remainder or "}}" in remainder:
        raise _malformed()
    return list(dict.fromkeys(found))


def validate_tokens(key: str, *texts: str) -> list[str]:
    """All tokens of ``texts``; 422 ``unknown_merge_field`` when any is unknown or not allowed for ``key``."""
    allowed = ALLOWED_GROUPS_BY_KEY.get(key, ALLOWED_GROUPS_BY_KEY["custom"])
    used: list[str] = []
    for text in texts:
        used.extend(tokens(text))
    used = list(dict.fromkeys(used))
    bad = [p for p in used if p not in MERGE_FIELDS or p.split(".", 1)[0] not in allowed]
    if bad:
        raise ValidationFailed(
            f"Unknown or disallowed merge fields: {', '.join(bad)}",
            code="unknown_merge_field",
            details={"fields": bad},
        )
    return used


def groups_of(paths: Iterable[str]) -> set[str]:
    return {p.split(".", 1)[0] for p in paths}


def render(text: str, data: MergeData, *, escape: bool) -> tuple[str, list[str]]:
    """Substitute every token (unknown ones render empty too). Returns ``(text, missing_paths)``."""
    missing: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        field = FIELD_RE.match(match.group(1))
        path = f"{field.group(1)}.{field.group(2)}" if field else match.group(1).strip()
        definition = MERGE_FIELDS.get(path)
        value = definition.resolver(data) if definition is not None else None
        if value is None or value == "":
            if path not in missing:
                missing.append(path)
            return ""
        return html.escape(value, quote=True) if escape else value

    return TOKEN_RE.sub(_sub, text or ""), missing


def sample_data() -> dict[str, dict[str, str | None]]:
    out: dict[str, dict[str, str | None]] = {}
    for path, field in MERGE_FIELDS.items():
        group, name = path.split(".", 1)
        out.setdefault(group, {})[name] = field.sample
    return out


def catalog_for(key: str | None) -> list[dict[str, object]]:
    """Grouped catalog (for the editor's "Insert field" menu), limited to ``key``'s allowed groups."""  # noqa: D401
    allowed = ALLOWED_GROUPS_BY_KEY.get(key or "custom", ALLOWED_GROUPS_BY_KEY["custom"])
    groups: dict[str, list[dict[str, str]]] = {}
    for path, field in MERGE_FIELDS.items():
        group = path.split(".", 1)[0]
        if group in allowed:
            groups.setdefault(group, []).append(
                {"path": path, "token": f"{{{{ {path} }}}}", "label": field.label, "sample": field.sample}
            )
    return [{"group": g, "label": GROUP_LABELS[g], "fields": fields} for g, fields in groups.items()]
